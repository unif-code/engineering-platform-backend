import hashlib
import json
from typing import Any

from control_plane.app.modules.requirement.application.common import (
    actor_id,
    audit,
    requirement_dto,
    work_item_dto,
)
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    AssignmentState,
    CreateRequirementResult,
    ExecutorType,
    RecordState,
    RepositoryBindingConflict,
    RepositoryState,
    RequirementDto,
    RequirementState,
    RequirementType,
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
from control_plane.app.modules.requirement.ports import RequirementRepository
from control_plane.app.shared.idempotency import (
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)


def _normalized_text(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidRequirementInput(f"{field} is required")
    return normalized


def _work_item_set_hash(work_item_id: str) -> str:
    value = json.dumps([work_item_id], separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


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
    state = derive_work_item_state(
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
    dependencies: RequirementDependencies,
) -> WorkItemDto:
    stable_actor = actor_id(actor)
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
