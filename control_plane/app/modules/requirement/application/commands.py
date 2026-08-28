import hashlib
import json
from datetime import datetime
from typing import Any

from control_plane.app.modules.audit import AuditEnvelope, record
from control_plane.app.modules.requirement.application.common import (
    actor_id,
    audit,
    decision_dto,
    gate_assignment_dto,
    gate_instance_dto,
    requirement_dto,
    sdd_baseline_dto,
    validated_correlation_id,
    work_item_dto,
)
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    ArtifactUnavailable,
    AssignmentState,
    BaselineConfirmationResult,
    BaselineDecisionResult,
    CreateRequirementResult,
    DecisionOutcome,
    ExecutorType,
    GateAlreadyDecided,
    GateAssignmentConflict,
    GateNotFound,
    GateReassignmentResult,
    GateReviewerIneligible,
    GateReviewerMismatch,
    GateState,
    GateType,
    RecordState,
    RegisterSddBaselineResult,
    RepositoryBindingBlockedReason,
    RepositoryBindingConflict,
    RepositoryBindingMessageInvalid,
    RepositoryBindingRequestMessage,
    RepositoryState,
    RequirementDependencyUnavailable,
    RequirementDto,
    RequirementError,
    RequirementState,
    RequirementType,
    SddBaselineNotFound,
    StaleBaselineSubject,
    StaleGateRevision,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    WorkItemDto,
    WorkItemNotFound,
    WorkItemState,
    derive_work_item_state,
    transition_requirement,
)
from control_plane.app.modules.requirement.domain.transitions import (
    InvalidRequirementInput,
    RepositoryBindingRequestMissing,
    RequirementNotFound,
)
from control_plane.app.modules.requirement.ports import (
    ArtifactState,
    ArtifactTrust,
    RequirementRepository,
)
from control_plane.app.shared.api.request_id import current_request_id
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)

_BINDING_RELEASE_ERROR_CODES = frozenset(
    {
        "BINDING_REQUEST_CONFLICT",
        "BINDING_REQUEST_INVALID",
        "SOURCE_CONTROL_UNAVAILABLE",
    }
)


def _normalized_text(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidRequirementInput(f"{field} is required")
    return normalized


def _is_canonical_sha256(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError:
        return False
    return True


def _work_item_set_hash(work_item_id: str) -> str:
    value = json.dumps([work_item_id], separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _audit_denial(
    *,
    dependencies: RequirementDependencies,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    error: Exception,
) -> None:
    record(
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor,
            actor_type="HUMAN",
            action=f"{action}_denied",
            target_type=target_type,
            target_id=target_id,
            result="DENIED",
            reason=f"reasonCode={type(error).__name__.upper()}",
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.denial_audit,
    )


def _create_requirement_once(
    repository: RequirementRepository,
    *,
    workspace_id: str,
    requirement_type: RequirementType,
    title: str,
    description: str,
    acceptance_criteria: tuple[str, ...],
    initial_repository_id: str,
    actor: Any,
    dependencies: RequirementDependencies,
) -> CreateRequirementResult:
    normalized_title = _normalized_text(title, field="title")
    normalized_description = _normalized_text(description, field="description")
    normalized_criteria = tuple(
        _normalized_text(value, field="acceptance criterion") for value in acceptance_criteria
    )
    if not normalized_criteria:
        raise InvalidRequirementInput("acceptance criteria are required")
    stable_actor = actor_id(actor)
    route = dependencies.route_snapshots.current(requirement_type)
    requirement_id = str(dependencies.random.uuid4())
    work_item_id = str(dependencies.random.uuid4())
    now = dependencies.clock.now()
    assigned = dependencies.assignment_guard.can_auto_assign(
        actor_id=stable_actor,
        workspace_id=workspace_id,
        repository_id=initial_repository_id,
        required_capabilities=route.required_capabilities,
    )
    owner_id = stable_actor if assigned else None
    assignment_state = AssignmentState.ASSIGNED if assigned else AssignmentState.UNASSIGNED
    requirement_row = repository.insert_requirement(
        id=requirement_id,
        workspace_id=workspace_id,
        type=requirement_type.value,
        title=normalized_title,
        description=normalized_description,
        acceptance_criteria=normalized_criteria,
        created_by=stable_actor,
        initial_repository_id=initial_repository_id,
        route_snapshot_version=route.version,
        route_snapshot_hash=route.snapshot_hash,
        route_snapshot={
            "requirementType": requirement_type.value,
            "requiredCapabilities": list(route.required_capabilities),
            "steps": list(route.steps),
            "version": route.version,
        },
        state=RequirementState.CREATED.value,
        record_state=RecordState.ACTIVE.value,
        requirement_version=1,
        required_work_item_set_version=1,
        required_work_item_set_hash=_work_item_set_hash(work_item_id),
        revision=1,
        now=now,
    )
    work_item_row = repository.insert_work_item(
        id=work_item_id,
        requirement_id=requirement_id,
        created_by=stable_actor,
        human_owner_id=owner_id,
        executor_type=ExecutorType.HUMAN.value,
        executor_id=owner_id,
        required_capabilities=route.required_capabilities,
        assignment_state=assignment_state.value,
        repository_state=RepositoryState.WAITING_REPOSITORY.value,
        state=WorkItemState.DRAFT.value,
        repository_id=initial_repository_id,
        revision=1,
        now=now,
    )
    if owner_id is not None:
        repository.insert_work_item_assignment(
            id=work_item_id,
            work_item_id=work_item_id,
            assignee_id=owner_id,
            assigned_by=stable_actor,
            reason="V0.4_INITIAL_ASSIGNMENT",
            revision=1,
            now=now,
        )
    repository.insert_outbox(
        id=str(dependencies.random.uuid4()),
        topic="requirement.repository-binding.requested",
        aggregate_type="REQUIREMENT",
        aggregate_id=requirement_id,
        aggregate_version=1,
        payload={"workItemId": work_item_id, "repositoryId": initial_repository_id},
        now=now,
    )
    audit(
        repository,
        dependencies=dependencies,
        actor=stable_actor,
        action="requirement.created",
        target_type="REQUIREMENT",
        target_id=requirement_id,
        reason="Requirement and initial binding request created; version=1",
    )
    audit(
        repository,
        dependencies=dependencies,
        actor=stable_actor,
        action="requirement.work_item.initialized",
        target_type="WORK_ITEM",
        target_id=work_item_id,
        reason=f"assignment={assignment_state.value}; repository=WAITING_REPOSITORY",
    )
    return CreateRequirementResult(
        requirement=requirement_dto(requirement_row),
        work_item=work_item_dto(work_item_row),
    )


def create_requirement(
    repository: RequirementRepository,
    *,
    workspace_id: str,
    requirement_type: RequirementType,
    title: str,
    description: str,
    acceptance_criteria: tuple[str, ...],
    initial_repository_id: str,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> CreateRequirementResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "workspaceId": workspace_id,
        "type": requirement_type.value,
        "title": title,
        "description": description,
        "acceptanceCriteria": list(acceptance_criteria),
        "initialRepositoryId": initial_repository_id,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_create",
        method="COMMAND",
        path="requirement.create",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        created = _create_requirement_once(
            repository,
            workspace_id=workspace_id,
            requirement_type=requirement_type,
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria,
            initial_repository_id=initial_repository_id,
            actor=actor,
            dependencies=dependencies,
        )
        return IdempotentResponse(
            status_code=201,
            body=created.model_dump(mode="json"),
        )

    execution = execute_idempotent(
        repository,
        actor=stable_actor,
        operation="requirement_create",
        key=idempotency_key,
        fingerprint=fingerprint,
        command=command,
        now=dependencies.clock.now,
        new_id=dependencies.random.uuid4,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    return CreateRequirementResult.model_validate(execution.response.body)


def claim_repository_binding_requests(
    repository: RequirementRepository,
    *,
    limit: int,
    available_before: datetime,
    lease_until: datetime,
) -> tuple[RepositoryBindingRequestMessage, ...]:
    rows = repository.claim_binding_requests(
        limit=limit,
        available_before=available_before,
        lease_until=lease_until,
    )
    messages: list[RepositoryBindingRequestMessage] = []
    for row in rows:
        payload = row["payload"]
        if (
            row["aggregate_type"] != "REQUIREMENT"
            or not isinstance(payload, dict)
            or set(payload) != {"workItemId", "repositoryId"}
            or not all(
                isinstance(payload[field], str) and payload[field].strip()
                for field in ("workItemId", "repositoryId")
            )
        ):
            raise RepositoryBindingMessageInvalid(str(row["id"]))
        messages.append(
            RepositoryBindingRequestMessage(
                message_id=str(row["id"]),
                requirement_id=str(row["aggregate_id"]),
                requirement_version=row["aggregate_version"],
                work_item_id=payload["workItemId"],
                repository_id=payload["repositoryId"],
                attempts=row["attempts"],
            )
        )
    return tuple(messages)


def acknowledge_repository_binding_request(
    repository: RequirementRepository,
    *,
    message_id: str,
    consumer: str,
    dependencies: RequirementDependencies,
) -> RequirementDto:
    if consumer != "SOURCE_CONTROL":
        raise InvalidRequirementInput("repository binding consumer is invalid")
    message = repository.outbox_by_id(message_id, for_update=True)
    if message is None or message["topic"] != "requirement.repository-binding.requested":
        raise RepositoryBindingRequestMissing(message_id)
    requirement = repository.requirement_by_id(
        str(message["aggregate_id"]),
        for_update=True,
    )
    if requirement is None:
        raise RequirementNotFound(str(message["aggregate_id"]))
    if message["state"] != "PUBLISHED":
        published = repository.publish_outbox(message_id, now=dependencies.clock.now())
        if published is None:
            raise RepositoryBindingRequestMissing(message_id)
    if RequirementState(requirement["state"]) is RequirementState.CREATED:
        updated = repository.update_requirement_state(
            str(requirement["id"]),
            expected_revision=requirement["revision"],
            state=RequirementState.PREPARING.value,
            now=dependencies.clock.now(),
        )
        if updated is None:
            raise StaleRequirementRevision(str(requirement["id"]))
        audit(
            repository,
            dependencies=dependencies,
            actor="SYSTEM",
            action="requirement.repository_binding.request_acknowledged",
            target_type="REQUIREMENT",
            target_id=str(requirement["id"]),
            reason=f"consumer={consumer}; messageId={message_id}",
        )
        requirement = updated
    return requirement_dto(requirement)


def release_repository_binding_request(
    repository: RequirementRepository,
    *,
    message_id: str,
    error_code: str,
    available_at: datetime,
) -> None:
    if error_code not in _BINDING_RELEASE_ERROR_CODES:
        raise InvalidRequirementInput("repository binding release error code is invalid")
    message = repository.outbox_by_id(message_id, for_update=True)
    if message is None or message["topic"] != "requirement.repository-binding.requested":
        raise RepositoryBindingRequestMissing(message_id)
    if message["state"] == "PUBLISHED":
        return
    released = repository.release_outbox(
        message_id,
        error_code=error_code,
        available_at=available_at,
    )
    if released is None:
        raise RepositoryBindingRequestMissing(message_id)


def _start_requirement_preparation_once(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    expected_revision: int,
    actor: Any,
    dependencies: RequirementDependencies,
) -> RequirementDto:
    row = repository.requirement_by_id(requirement_id, for_update=True)
    if row is None:
        raise RequirementNotFound(requirement_id)
    if row["revision"] != expected_revision:
        raise StaleRequirementRevision(requirement_id)
    messages = repository.outbox_by_aggregate(
        requirement_id,
        aggregate_version=row["requirement_version"],
    )
    if not any(
        message["topic"] == "requirement.repository-binding.requested" for message in messages
    ):
        raise RepositoryBindingRequestMissing(requirement_id)
    target = transition_requirement(
        RequirementState(row["state"]),
        RequirementState.PREPARING,
    )
    updated = repository.update_requirement_state(
        requirement_id,
        expected_revision=expected_revision,
        state=target.value,
        now=dependencies.clock.now(),
    )
    if updated is None:
        raise StaleRequirementRevision(requirement_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_id(actor),
        action="requirement.preparation.started",
        target_type="REQUIREMENT",
        target_id=requirement_id,
        reason=f"binding request durable; revision={updated['revision']}",
    )
    return requirement_dto(updated)


def start_requirement_preparation(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> RequirementDto:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "requirementId": requirement_id,
        "expectedRevision": expected_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_start_preparation",
        method="COMMAND",
        path="requirement.start-preparation",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        prepared = _start_requirement_preparation_once(
            repository,
            requirement_id=requirement_id,
            expected_revision=expected_revision,
            actor=actor,
            dependencies=dependencies,
        )
        return IdempotentResponse(
            status_code=200,
            body=prepared.model_dump(mode="json"),
        )

    execution = execute_idempotent(
        repository,
        actor=stable_actor,
        operation="requirement_start_preparation",
        key=idempotency_key,
        fingerprint=fingerprint,
        command=command,
        now=dependencies.clock.now,
        new_id=dependencies.random.uuid4,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    return RequirementDto.model_validate(execution.response.body)


def _record_repository_binding_once(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    repository_id: str,
    base_commit_sha: str,
    task_branch: str,
    expected_revision: int,
    actor: Any,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> WorkItemDto:
    row = repository.work_item_by_id(work_item_id, for_update=True)
    if row is None:
        raise WorkItemNotFound(work_item_id)
    if row["repository_id"] != repository_id:
        raise RepositoryBindingConflict("Repository binding targets a different repository")
    normalized_sha = _normalized_text(base_commit_sha, field="base commit SHA")
    normalized_branch = _normalized_text(task_branch, field="task branch")
    if row["repository_state"] == RepositoryState.BOUND.value:
        if row["base_commit_sha"] == normalized_sha and row["task_branch"] == normalized_branch:
            return work_item_dto(row)
        raise RepositoryBindingConflict("WorkItem already has a different repository binding")
    if row["revision"] != expected_revision:
        raise StaleWorkItemRevision(work_item_id)
    requirement = repository.requirement_by_id(str(row["requirement_id"]))
    if requirement is None:
        raise RequirementNotFound(str(row["requirement_id"]))
    state = derive_work_item_state(
        RequirementState(requirement["state"]),
        AssignmentState(row["assignment_state"]),
        RepositoryState.BOUND,
    )
    updated = repository.bind_work_item(
        work_item_id,
        expected_revision=expected_revision,
        base_commit_sha=normalized_sha,
        task_branch=normalized_branch,
        state=state.value,
        now=dependencies.clock.now(),
    )
    if updated is None:
        raise StaleWorkItemRevision(work_item_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_id(actor),
        action="requirement.repository_binding.recorded",
        target_type="WORK_ITEM",
        target_id=work_item_id,
        reason=f"repository={repository_id}; revision={updated['revision']}",
        correlation_id=correlation_id,
    )
    return work_item_dto(updated)


def record_repository_binding(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    repository_id: str,
    base_commit_sha: str,
    task_branch: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> WorkItemDto:
    stable_actor = actor_id(actor)
    stable_correlation_id = validated_correlation_id(correlation_id)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "workItemId": work_item_id,
        "repositoryId": repository_id,
        "baseCommitSha": base_commit_sha,
        "taskBranch": task_branch,
        "expectedRevision": expected_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_record_repository_binding",
        method="COMMAND",
        path="requirement.record-repository-binding",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        bound = _record_repository_binding_once(
            repository,
            work_item_id=work_item_id,
            repository_id=repository_id,
            base_commit_sha=base_commit_sha,
            task_branch=task_branch,
            expected_revision=expected_revision,
            actor=actor,
            correlation_id=stable_correlation_id,
            dependencies=dependencies,
        )
        return IdempotentResponse(status_code=200, body=bound.model_dump(mode="json"))

    execution = execute_idempotent(
        repository,
        actor=stable_actor,
        operation="requirement_record_repository_binding",
        key=idempotency_key,
        fingerprint=fingerprint,
        command=command,
        now=dependencies.clock.now,
        new_id=dependencies.random.uuid4,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    return WorkItemDto.model_validate(execution.response.body)


def _record_repository_binding_blocked_once(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    repository_id: str,
    reason_code: RepositoryBindingBlockedReason,
    expected_revision: int,
    actor: Any,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> WorkItemDto:
    row = repository.work_item_by_id(work_item_id, for_update=True)
    if row is None:
        raise WorkItemNotFound(work_item_id)
    if row["repository_id"] != repository_id:
        raise RepositoryBindingConflict("Repository block targets a different repository")
    if row["repository_state"] == RepositoryState.BOUND.value:
        raise RepositoryBindingConflict("A bound WorkItem cannot become repository-blocked")
    if (
        row["repository_state"] == RepositoryState.BLOCKED.value
        and row["repository_blocked_reason_code"] == reason_code.value
    ):
        return work_item_dto(row)
    if row["revision"] != expected_revision:
        raise StaleWorkItemRevision(work_item_id)
    updated = repository.block_work_item(
        work_item_id,
        expected_revision=expected_revision,
        reason_code=reason_code.value,
        now=dependencies.clock.now(),
    )
    if updated is None:
        raise StaleWorkItemRevision(work_item_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_id(actor),
        action="requirement.repository_binding.blocked",
        target_type="WORK_ITEM",
        target_id=work_item_id,
        reason=(
            f"repository={repository_id}; reasonCode={reason_code.value}; "
            f"revision={updated['revision']}"
        ),
        correlation_id=correlation_id,
    )
    return work_item_dto(updated)


def record_repository_binding_blocked(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    repository_id: str,
    reason_code: RepositoryBindingBlockedReason,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> WorkItemDto:
    stable_actor = actor_id(actor)
    stable_correlation_id = validated_correlation_id(correlation_id)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "workItemId": work_item_id,
        "repositoryId": repository_id,
        "reasonCode": reason_code.value,
        "expectedRevision": expected_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_record_repository_binding_blocked",
        method="COMMAND",
        path="requirement.record-repository-binding-blocked",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        blocked = _record_repository_binding_blocked_once(
            repository,
            work_item_id=work_item_id,
            repository_id=repository_id,
            reason_code=reason_code,
            expected_revision=expected_revision,
            actor=actor,
            correlation_id=stable_correlation_id,
            dependencies=dependencies,
        )
        return IdempotentResponse(status_code=200, body=blocked.model_dump(mode="json"))

    execution = execute_idempotent(
        repository,
        actor=stable_actor,
        operation="requirement_record_repository_binding_blocked",
        key=idempotency_key,
        fingerprint=fingerprint,
        command=command,
        now=dependencies.clock.now,
        new_id=dependencies.random.uuid4,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    return WorkItemDto.model_validate(execution.response.body)


def _register_sdd_baseline_once(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    artifact_id: str,
    artifact_version: str,
    expected_revision: int,
    actor: Any,
    dependencies: RequirementDependencies,
) -> RegisterSddBaselineResult:
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    if requirement["revision"] != expected_revision:
        raise StaleRequirementRevision(requirement_id)
    if RequirementState(requirement["state"]) is not RequirementState.PREPARING:
        raise StaleBaselineSubject("Requirement is not preparing an SDD baseline")
    artifacts = dependencies.artifacts
    if artifacts is None:
        raise RequirementDependencyUnavailable("Artifact service is unavailable")
    normalized_artifact_id = _normalized_text(artifact_id, field="artifact id")
    normalized_artifact_version = _normalized_text(
        artifact_version,
        field="artifact version",
    )
    try:
        snapshot = artifacts.get_snapshot(
            requirement_id,
            normalized_artifact_id,
            normalized_artifact_version,
        )
    except RequirementError:
        raise
    except Exception as error:
        raise RequirementDependencyUnavailable("Artifact service failed closed") from error
    if (
        snapshot.state is not ArtifactState.AVAILABLE
        or snapshot.trust is not ArtifactTrust.TRUSTED_PLAIN_TEXT
        or not snapshot.media_type.split(";", 1)[0].strip().startswith("text/")
        or snapshot.id != normalized_artifact_id
        or snapshot.version != normalized_artifact_version
        or not snapshot.sha256
    ):
        raise ArtifactUnavailable(normalized_artifact_id)
    existing = repository.sdd_baseline_by_artifact(
        requirement_id,
        snapshot.id,
        snapshot.version,
    )
    if existing is not None:
        if existing["artifact_hash"] != snapshot.sha256:
            raise ArtifactUnavailable("Artifact identity is not immutable")
        if (
            requirement["current_sdd_baseline_id"] is None
            or str(requirement["current_sdd_baseline_id"]) != str(existing["id"])
            or repository.gate_by_baseline_id(str(existing["id"])) is not None
        ):
            raise StaleBaselineSubject("A used Artifact version cannot become current again")
        return RegisterSddBaselineResult(
            requirement=requirement_dto(requirement),
            baseline=sdd_baseline_dto(existing),
        )
    stable_actor = actor_id(actor)
    baseline = repository.insert_sdd_baseline(
        id=str(dependencies.random.uuid4()),
        requirement_id=requirement_id,
        requirement_version=requirement["requirement_version"],
        artifact_id=snapshot.id,
        artifact_version=snapshot.version,
        artifact_hash=snapshot.sha256,
        route_snapshot_version=requirement["route_snapshot_version"],
        route_snapshot_hash=requirement["route_snapshot_hash"],
        created_by=stable_actor,
        now=dependencies.clock.now(),
    )
    updated = repository.set_current_sdd_baseline(
        requirement_id,
        baseline_id=str(baseline["id"]),
        expected_revision=expected_revision,
        now=dependencies.clock.now(),
    )
    if updated is None:
        raise StaleRequirementRevision(requirement_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=stable_actor,
        action="requirement.sdd_baseline.registered",
        target_type="SDD_BASELINE",
        target_id=str(baseline["id"]),
        reason=(
            f"requirementVersion={requirement['requirement_version']}; "
            f"artifact={snapshot.id}@{snapshot.version}; revision={updated['revision']}"
        ),
    )
    return RegisterSddBaselineResult(
        requirement=requirement_dto(updated),
        baseline=sdd_baseline_dto(baseline),
    )


def register_sdd_baseline(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    artifact_id: str,
    artifact_version: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> RegisterSddBaselineResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "requirementId": requirement_id,
        "artifactId": artifact_id,
        "artifactVersion": artifact_version,
        "expectedRevision": expected_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_register_sdd_baseline",
        method="COMMAND",
        path="requirement.register-sdd-baseline",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        try:
            registered = _register_sdd_baseline_once(
                repository,
                requirement_id=requirement_id,
                artifact_id=artifact_id,
                artifact_version=artifact_version,
                expected_revision=expected_revision,
                actor=actor,
                dependencies=dependencies,
            )
        except RequirementError as error:
            _audit_denial(
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.sdd_baseline.register",
                target_type="REQUIREMENT",
                target_id=requirement_id,
                error=error,
            )
            raise
        return IdempotentResponse(status_code=201, body=registered.model_dump(mode="json"))

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation="requirement_register_sdd_baseline",
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except IdempotencyConflict as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.sdd_baseline.register",
            target_type="REQUIREMENT",
            target_id=requirement_id,
            error=error,
        )
        raise
    return RegisterSddBaselineResult.model_validate(execution.response.body)


def _submit_baseline_confirmation_once(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    sdd_baseline_id: str,
    expected_revision: int,
    actor: Any,
    dependencies: RequirementDependencies,
) -> BaselineConfirmationResult:
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    if requirement["revision"] != expected_revision:
        raise StaleRequirementRevision(requirement_id)
    baseline = repository.sdd_baseline_by_id(sdd_baseline_id)
    if baseline is None:
        raise SddBaselineNotFound(sdd_baseline_id)
    if (
        requirement["current_sdd_baseline_id"] is None
        or str(requirement["current_sdd_baseline_id"]) != sdd_baseline_id
    ):
        raise StaleBaselineSubject("Only the current SDD baseline can be submitted")
    subject = (
        str(baseline["requirement_id"]),
        baseline["requirement_version"],
        baseline["route_snapshot_version"],
        baseline["route_snapshot_hash"],
    )
    current = (
        requirement_id,
        requirement["requirement_version"],
        requirement["route_snapshot_version"],
        requirement["route_snapshot_hash"],
    )
    if subject != current:
        raise StaleBaselineSubject(sdd_baseline_id)
    if repository.gate_by_baseline_id(sdd_baseline_id) is not None:
        raise StaleBaselineSubject("An SDD baseline cannot be submitted twice")
    target = transition_requirement(
        RequirementState(requirement["state"]),
        RequirementState.AWAITING_CONFIRMATION,
    )
    gate_policies = dependencies.gate_policies
    if gate_policies is None:
        raise RequirementDependencyUnavailable("Gate policy service is unavailable")
    try:
        policy = gate_policies.requirement_baseline(workspace_id=requirement["workspace_id"])
    except RequirementError:
        raise
    except Exception as error:
        raise RequirementDependencyUnavailable("Gate policy service failed closed") from error
    if (
        policy.version < 1
        or not policy.default_reviewer_id.strip()
        or not policy.policy_code.strip()
        or not _is_canonical_sha256(policy.snapshot_hash)
    ):
        raise RequirementDependencyUnavailable("Gate policy snapshot is invalid")
    now = dependencies.clock.now()
    gate = repository.insert_gate(
        id=str(dependencies.random.uuid4()),
        gate_type=GateType.REQUIREMENT_BASELINE_CONFIRMATION.value,
        requirement_id=requirement_id,
        requirement_version=baseline["requirement_version"],
        sdd_baseline_id=sdd_baseline_id,
        artifact_id=baseline["artifact_id"],
        artifact_version=baseline["artifact_version"],
        artifact_hash=baseline["artifact_hash"],
        route_snapshot_version=baseline["route_snapshot_version"],
        route_snapshot_hash=baseline["route_snapshot_hash"],
        policy_code=policy.policy_code,
        policy_version=policy.version,
        policy_snapshot_hash=policy.snapshot_hash,
        state=GateState.OPEN.value,
        revision=1,
        now=now,
    )
    assignment = repository.insert_gate_assignment(
        id=str(dependencies.random.uuid4()),
        gate_instance_id=str(gate["id"]),
        default_reviewer_id=policy.default_reviewer_id,
        current_reviewer_id=policy.default_reviewer_id,
        revision=1,
        now=now,
    )
    updated = repository.update_requirement_state(
        requirement_id,
        expected_revision=expected_revision,
        state=target.value,
        now=now,
    )
    if updated is None:
        raise StaleRequirementRevision(requirement_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_id(actor),
        action="requirement.baseline_confirmation.submitted",
        target_type="GATE_INSTANCE",
        target_id=str(gate["id"]),
        reason=(
            f"policyVersion={policy.version}; reviewer={policy.default_reviewer_id}; "
            f"requirementRevision={updated['revision']}"
        ),
    )
    return BaselineConfirmationResult(
        requirement=requirement_dto(updated),
        gate=gate_instance_dto(gate),
        assignment=gate_assignment_dto(assignment),
    )


def submit_baseline_confirmation(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    sdd_baseline_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> BaselineConfirmationResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "requirementId": requirement_id,
        "sddBaselineId": sdd_baseline_id,
        "expectedRevision": expected_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_submit_baseline_confirmation",
        method="COMMAND",
        path="requirement.submit-baseline-confirmation",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        try:
            confirmation = _submit_baseline_confirmation_once(
                repository,
                requirement_id=requirement_id,
                sdd_baseline_id=sdd_baseline_id,
                expected_revision=expected_revision,
                actor=actor,
                dependencies=dependencies,
            )
        except RequirementError as error:
            _audit_denial(
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.baseline_confirmation.submit",
                target_type="REQUIREMENT",
                target_id=requirement_id,
                error=error,
            )
            raise
        return IdempotentResponse(
            status_code=201,
            body=confirmation.model_dump(mode="json"),
        )

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation="requirement_submit_baseline_confirmation",
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except IdempotencyConflict as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.baseline_confirmation.submit",
            target_type="REQUIREMENT",
            target_id=requirement_id,
            error=error,
        )
        raise
    return BaselineConfirmationResult.model_validate(execution.response.body)


def _decision_target(outcome: DecisionOutcome) -> RequirementState:
    if outcome is DecisionOutcome.APPROVED:
        return RequirementState.READY
    if outcome is DecisionOutcome.CHANGES_REQUESTED:
        return RequirementState.PREPARING
    return RequirementState.CANCELED


def _reassign_baseline_gate_once(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    gate_id: str,
    reviewer_id: str,
    reason: str,
    expected_gate_revision: int,
    actor: Any,
    dependencies: RequirementDependencies,
) -> GateReassignmentResult:
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    gate = repository.gate_by_id(gate_id, for_update=True)
    if gate is None or str(gate["requirement_id"]) != requirement_id:
        raise GateNotFound(gate_id)
    if gate["revision"] != expected_gate_revision:
        raise StaleGateRevision(gate_id)
    if GateState(gate["state"]) is not GateState.OPEN:
        raise GateAlreadyDecided(gate_id)
    current = repository.current_gate_assignment(gate_id, for_update=True)
    if current is None:
        raise RequirementDependencyUnavailable("Gate has no current reviewer assignment")
    stable_actor = actor_id(actor)
    if current["default_reviewer_id"] != stable_actor:
        raise GateReviewerMismatch(stable_actor)
    candidate_id = _normalized_text(reviewer_id, field="reviewer id")
    normalized_reason = _normalized_text(reason, field="reassignment reason")
    if current["current_reviewer_id"] == candidate_id:
        raise GateAssignmentConflict("Candidate is already the current reviewer")
    reviewer_guard = dependencies.reviewer_guard
    if reviewer_guard is None:
        raise RequirementDependencyUnavailable("Reviewer eligibility service is unavailable")
    try:
        candidate_eligible = reviewer_guard.can_decide(
            actor_id=candidate_id,
            workspace_id=str(requirement["workspace_id"]),
        )
    except RequirementError:
        raise
    except Exception as error:
        raise RequirementDependencyUnavailable("Reviewer guard failed closed") from error
    if not candidate_eligible:
        raise GateReviewerIneligible(candidate_id)

    now = dependencies.clock.now()
    superseded = repository.supersede_gate_assignment(
        str(current["id"]),
        expected_revision=current["revision"],
        now=now,
    )
    if superseded is None:
        raise StaleGateRevision(gate_id)
    assignment = repository.insert_gate_assignment(
        id=str(dependencies.random.uuid4()),
        gate_instance_id=gate_id,
        default_reviewer_id=current["default_reviewer_id"],
        current_reviewer_id=candidate_id,
        revision=current["revision"] + 1,
        now=now,
    )
    updated_gate = repository.reassign_gate(
        gate_id,
        expected_revision=expected_gate_revision,
    )
    if updated_gate is None:
        raise StaleGateRevision(gate_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=stable_actor,
        action="requirement.baseline_gate.reassigned",
        target_type="GATE_INSTANCE",
        target_id=gate_id,
        reason=(
            f"reviewer={candidate_id}; assignmentRevision={assignment['revision']}; "
            f"gateRevision={updated_gate['revision']}; reason={normalized_reason}"
        ),
    )
    return GateReassignmentResult(
        gate=gate_instance_dto(updated_gate),
        assignment=gate_assignment_dto(assignment),
    )


def reassign_baseline_gate(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    gate_id: str,
    reviewer_id: str,
    reason: str,
    expected_gate_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> GateReassignmentResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "requirementId": requirement_id,
        "gateId": gate_id,
        "reviewerId": reviewer_id,
        "reason": reason,
        "expectedGateRevision": expected_gate_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_reassign_baseline_gate",
        method="COMMAND",
        path="requirement.reassign-baseline-gate",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        try:
            result = _reassign_baseline_gate_once(
                repository,
                requirement_id=requirement_id,
                gate_id=gate_id,
                reviewer_id=reviewer_id,
                reason=reason,
                expected_gate_revision=expected_gate_revision,
                actor=actor,
                dependencies=dependencies,
            )
        except RequirementError as error:
            _audit_denial(
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.baseline_gate.reassign",
                target_type="GATE_INSTANCE",
                target_id=gate_id,
                error=error,
            )
            raise
        return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation="requirement_reassign_baseline_gate",
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except IdempotencyConflict as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.baseline_gate.reassign",
            target_type="GATE_INSTANCE",
            target_id=gate_id,
            error=error,
        )
        raise
    return GateReassignmentResult.model_validate(execution.response.body)


def _decide_baseline_once(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    gate_id: str,
    outcome: DecisionOutcome,
    reason: str,
    expected_revision: int,
    actor: Any,
    dependencies: RequirementDependencies,
) -> BaselineDecisionResult:
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    if requirement["revision"] != expected_revision:
        raise StaleRequirementRevision(requirement_id)
    gate = repository.gate_by_id(gate_id, for_update=True)
    if gate is None or str(gate["requirement_id"]) != requirement_id:
        raise GateNotFound(gate_id)
    if GateState(gate["state"]) is not GateState.OPEN:
        raise GateAlreadyDecided(gate_id)
    if (
        gate["requirement_version"] != requirement["requirement_version"]
        or gate["route_snapshot_version"] != requirement["route_snapshot_version"]
        or gate["route_snapshot_hash"] != requirement["route_snapshot_hash"]
    ):
        raise StaleBaselineSubject(gate_id)
    assignment = repository.current_gate_assignment(gate_id, for_update=True)
    if assignment is None:
        raise RequirementDependencyUnavailable("Gate has no current reviewer assignment")
    stable_actor = actor_id(actor)
    if assignment["current_reviewer_id"] != stable_actor:
        raise GateReviewerMismatch(stable_actor)
    reviewer_guard = dependencies.reviewer_guard
    if reviewer_guard is None:
        raise RequirementDependencyUnavailable("Reviewer eligibility service is unavailable")
    try:
        reviewer_eligible = reviewer_guard.can_decide(
            actor_id=stable_actor,
            workspace_id=requirement["workspace_id"],
        )
    except RequirementError:
        raise
    except Exception as error:
        raise RequirementDependencyUnavailable("Reviewer guard failed closed") from error
    if not reviewer_eligible:
        raise GateReviewerIneligible(stable_actor)
    normalized_reason = _normalized_text(reason, field="decision reason")
    target = transition_requirement(
        RequirementState(requirement["state"]),
        _decision_target(outcome),
    )
    now = dependencies.clock.now()
    decision = repository.insert_decision(
        id=str(dependencies.random.uuid4()),
        gate_instance_id=gate_id,
        gate_assignment_id=str(assignment["id"]),
        reviewer_id=stable_actor,
        outcome=outcome.value,
        reason=normalized_reason,
        subject_revision=gate["revision"],
        now=now,
    )
    closed_gate = repository.close_gate(
        gate_id,
        expected_revision=gate["revision"],
        now=now,
    )
    if closed_gate is None:
        raise GateAlreadyDecided(gate_id)
    updated = repository.update_requirement_state(
        requirement_id,
        expected_revision=expected_revision,
        state=target.value,
        now=now,
    )
    if updated is None:
        raise StaleRequirementRevision(requirement_id)
    repository.reconcile_planned_work_item_states(
        requirement_id,
        requirement_state=target.value,
        now=now,
    )
    audit(
        repository,
        dependencies=dependencies,
        actor=stable_actor,
        action="requirement.baseline_confirmation.decided",
        target_type="GATE_INSTANCE",
        target_id=gate_id,
        reason=(
            f"outcome={outcome.value}; assignmentRevision={assignment['revision']}; "
            f"requirementRevision={updated['revision']}"
        ),
    )
    return BaselineDecisionResult(
        requirement=requirement_dto(updated),
        gate=gate_instance_dto(closed_gate),
        decision=decision_dto(decision),
    )


def decide_baseline(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    gate_id: str,
    outcome: DecisionOutcome,
    reason: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> BaselineDecisionResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "requirementId": requirement_id,
        "gateId": gate_id,
        "outcome": outcome.value,
        "reason": reason,
        "expectedRevision": expected_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_decide_baseline",
        method="COMMAND",
        path="requirement.decide-baseline",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        try:
            decided = _decide_baseline_once(
                repository,
                requirement_id=requirement_id,
                gate_id=gate_id,
                outcome=outcome,
                reason=reason,
                expected_revision=expected_revision,
                actor=actor,
                dependencies=dependencies,
            )
        except RequirementError as error:
            _audit_denial(
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.baseline_confirmation.decide",
                target_type="GATE_INSTANCE",
                target_id=gate_id,
                error=error,
            )
            raise
        return IdempotentResponse(status_code=200, body=decided.model_dump(mode="json"))

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation="requirement_decide_baseline",
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except IdempotencyConflict as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.baseline_confirmation.decide",
            target_type="GATE_INSTANCE",
            target_id=gate_id,
            error=error,
        )
        raise
    return BaselineDecisionResult.model_validate(execution.response.body)
