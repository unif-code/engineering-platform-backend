from typing import Any

from control_plane.app.modules.audit import AuditEnvelope, record
from control_plane.app.modules.requirement.application.common import (
    actor_id,
    audit,
    requirement_dto,
    validate_frozen_route_snapshot,
    work_item_assignment_dto,
    work_item_dto,
)
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    AddWorkItemResult,
    AssignmentState,
    AssignWorkItemResult,
    ExecutorType,
    IntegrationDeliveryState,
    InvalidRequirementInput,
    RepositoryBindingBlockedReason,
    RepositoryState,
    RequirementDependencyUnavailable,
    RequirementError,
    RequirementNotFound,
    RequirementState,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    WorkItemAssigneeIneligible,
    WorkItemAssignmentConflict,
    WorkItemNotFound,
    WorkItemState,
    required_work_item_set_hash,
)
from control_plane.app.modules.requirement.ports import RequirementRepository
from control_plane.app.shared.api.request_id import current_request_id
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)

_OWNER_BINDING_BLOCKS = frozenset(
    {
        RepositoryBindingBlockedReason.OWNER_UNASSIGNED.value,
        RepositoryBindingBlockedReason.OWNER_INELIGIBLE.value,
    }
)


def _normalized_text(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidRequirementInput(f"{field} is required")
    return normalized


def _route_capabilities(requirement: Any) -> tuple[str, ...]:
    route = validate_frozen_route_snapshot(
        requirement["route_snapshot"],
        expected_hash=requirement["route_snapshot_hash"],
        expected_version=requirement["route_snapshot_version"],
        expected_requirement_type=requirement["type"],
    )
    raw = route.get("requiredCapabilities") if isinstance(route, dict) else None
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(value, str) or not value.strip() for value in raw)
    ):
        raise RequirementDependencyUnavailable("Frozen Route Snapshot is invalid")
    return tuple(raw)


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


def _add_work_item_once(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    repository_id: str,
    expected_revision: int,
    actor: Any,
    dependencies: RequirementDependencies,
) -> AddWorkItemResult:
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    if requirement["revision"] != expected_revision:
        raise StaleRequirementRevision(requirement_id)
    if RequirementState(requirement["state"]) is not RequirementState.PREPARING:
        raise WorkItemAssignmentConflict("WorkItems can only be added while preparing")

    stable_repository_id = _normalized_text(repository_id, field="repository ID")
    stable_actor = actor_id(actor)
    capabilities = _route_capabilities(requirement)
    try:
        auto_assigned = dependencies.assignment_guard.can_auto_assign(
            actor_id=stable_actor,
            workspace_id=str(requirement["workspace_id"]),
            repository_id=stable_repository_id,
            required_capabilities=capabilities,
        )
    except Exception:
        auto_assigned = False
    owner_id = stable_actor if auto_assigned else None
    assignment_state = AssignmentState.ASSIGNED if auto_assigned else AssignmentState.UNASSIGNED
    now = dependencies.clock.now()
    work_item_id = str(dependencies.random.uuid4())
    work_item = repository.insert_work_item(
        id=work_item_id,
        requirement_id=requirement_id,
        created_by=stable_actor,
        human_owner_id=owner_id,
        executor_type=ExecutorType.HUMAN.value,
        executor_id=owner_id,
        required_capabilities=capabilities,
        assignment_state=assignment_state.value,
        repository_state=RepositoryState.WAITING_REPOSITORY.value,
        state=WorkItemState.DRAFT.value,
        repository_id=stable_repository_id,
        revision=1,
        now=now,
    )
    assignment = None
    if owner_id is not None:
        assignment = repository.insert_work_item_assignment(
            id=work_item_id,
            work_item_id=work_item_id,
            assignee_id=owner_id,
            assigned_by=stable_actor,
            reason="V0.4_AUTO_ASSIGNMENT",
            revision=1,
            now=now,
        )
    work_item_ids = tuple(str(row["id"]) for row in repository.work_items(requirement_id))
    updated = repository.update_requirement_plan(
        requirement_id,
        expected_revision=expected_revision,
        required_work_item_set_hash=required_work_item_set_hash(work_item_ids),
        now=now,
    )
    if updated is None:
        raise StaleRequirementRevision(requirement_id)
    repository.insert_outbox(
        id=str(dependencies.random.uuid4()),
        topic="requirement.repository-binding.requested",
        aggregate_type="REQUIREMENT",
        aggregate_id=requirement_id,
        aggregate_version=updated["requirement_version"],
        payload={"workItemId": work_item_id, "repositoryId": stable_repository_id},
        now=now,
    )
    audit(
        repository,
        dependencies=dependencies,
        actor=stable_actor,
        action="requirement.work_item.added",
        target_type="WORK_ITEM",
        target_id=work_item_id,
        reason=(
            f"assignment={assignment_state.value}; "
            f"requiredWorkItemSetVersion={updated['required_work_item_set_version']}"
        ),
    )
    return AddWorkItemResult(
        requirement=requirement_dto(updated),
        work_item=work_item_dto(work_item),
        assignment=None if assignment is None else work_item_assignment_dto(assignment),
    )


def add_work_item(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    repository_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> AddWorkItemResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "requirementId": requirement_id,
        "repositoryId": repository_id,
        "expectedRevision": expected_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_add_work_item",
        method="COMMAND",
        path="requirement.add-work-item",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        try:
            result = _add_work_item_once(
                repository,
                requirement_id=requirement_id,
                repository_id=repository_id,
                expected_revision=expected_revision,
                actor=actor,
                dependencies=dependencies,
            )
        except RequirementError as error:
            _audit_denial(
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.work_item.add",
                target_type="REQUIREMENT",
                target_id=requirement_id,
                error=error,
            )
            raise
        return IdempotentResponse(status_code=201, body=result.model_dump(mode="json"))

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation="requirement_add_work_item",
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
            action="requirement.work_item.add",
            target_type="REQUIREMENT",
            target_id=requirement_id,
            error=error,
        )
        raise
    return AddWorkItemResult.model_validate(execution.response.body)


def _assign_work_item_once(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    work_item_id: str,
    human_owner_id: str,
    reason: str,
    expected_revision: int,
    actor: Any,
    dependencies: RequirementDependencies,
) -> AssignWorkItemResult:
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    if RequirementState(requirement["state"]) is not RequirementState.PREPARING:
        raise WorkItemAssignmentConflict("WorkItems can only be assigned while preparing")
    work_item = repository.work_item_by_id(work_item_id, for_update=True)
    if work_item is None or str(work_item["requirement_id"]) != requirement_id:
        raise WorkItemNotFound(work_item_id)
    if work_item["revision"] != expected_revision:
        raise StaleWorkItemRevision(work_item_id)
    if IntegrationDeliveryState(work_item["integration_delivery_state"]) is not (
        IntegrationDeliveryState.NOT_STARTED
    ):
        raise WorkItemAssignmentConflict("Started WorkItem responsibility is immutable")

    stable_actor = actor_id(actor)
    candidate_id = _normalized_text(human_owner_id, field="human owner ID")
    normalized_reason = _normalized_text(reason, field="assignment reason")
    try:
        explicit_guard = getattr(
            dependencies.assignment_guard,
            "can_assign",
            dependencies.assignment_guard.can_auto_assign,
        )
        eligible = explicit_guard(
            actor_id=candidate_id,
            workspace_id=str(requirement["workspace_id"]),
            repository_id=work_item["repository_id"],
            required_capabilities=tuple(work_item["required_capabilities"]),
        )
    except Exception as error:
        raise RequirementDependencyUnavailable("Assignment eligibility failed closed") from error
    if not eligible:
        raise WorkItemAssigneeIneligible(candidate_id)

    current = repository.current_work_item_assignment(work_item_id, for_update=True)
    if current is not None and current["assignee_id"] == candidate_id:
        raise WorkItemAssignmentConflict("Candidate is already the current assignee")
    assignment_revision = 1 if current is None else current["revision"] + 1
    previous_repository_state = RepositoryState(work_item["repository_state"])
    previous_blocked_reason = work_item["repository_blocked_reason_code"]
    owner_blocked = previous_blocked_reason in _OWNER_BINDING_BLOCKS
    repository_state = (
        RepositoryState.WAITING_REPOSITORY if owner_blocked else previous_repository_state
    )
    blocked_reason = None if owner_blocked else previous_blocked_reason
    blocked_at = None if owner_blocked else work_item["repository_blocked_at"]
    now = dependencies.clock.now()
    if current is not None:
        superseded = repository.supersede_work_item_assignment(
            str(current["id"]),
            expected_revision=current["revision"],
            now=now,
        )
        if superseded is None:
            raise StaleWorkItemRevision(work_item_id)
    updated = repository.assign_work_item_projection(
        work_item_id,
        expected_revision=expected_revision,
        human_owner_id=candidate_id,
        repository_state=repository_state.value,
        repository_blocked_reason_code=blocked_reason,
        repository_blocked_at=blocked_at,
        now=now,
    )
    if updated is None:
        raise StaleWorkItemRevision(work_item_id)
    assignment = repository.insert_work_item_assignment(
        id=str(dependencies.random.uuid4()),
        work_item_id=work_item_id,
        assignee_id=candidate_id,
        assigned_by=stable_actor,
        reason=normalized_reason,
        revision=assignment_revision,
        now=now,
    )
    if previous_repository_state is not RepositoryState.BOUND and (
        AssignmentState(work_item["assignment_state"]) is AssignmentState.UNASSIGNED
        or owner_blocked
    ):
        repository.insert_outbox(
            id=str(dependencies.random.uuid4()),
            topic="requirement.repository-binding.requested",
            aggregate_type="REQUIREMENT",
            aggregate_id=requirement_id,
            aggregate_version=requirement["requirement_version"],
            payload={"workItemId": work_item_id, "repositoryId": work_item["repository_id"]},
            now=now,
        )
    audit(
        repository,
        dependencies=dependencies,
        actor=stable_actor,
        action="requirement.work_item.assigned",
        target_type="WORK_ITEM",
        target_id=work_item_id,
        reason=(
            f"assignee={candidate_id}; assignmentRevision={assignment_revision}; "
            f"workItemRevision={updated['revision']}"
        ),
    )
    return AssignWorkItemResult(
        work_item=work_item_dto(updated),
        assignment=work_item_assignment_dto(assignment),
    )


def assign_work_item(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    work_item_id: str,
    human_owner_id: str,
    reason: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> AssignWorkItemResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "requirementId": requirement_id,
        "workItemId": work_item_id,
        "humanOwnerId": human_owner_id,
        "reason": reason,
        "expectedRevision": expected_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_assign_work_item",
        method="COMMAND",
        path="requirement.assign-work-item",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        try:
            result = _assign_work_item_once(
                repository,
                requirement_id=requirement_id,
                work_item_id=work_item_id,
                human_owner_id=human_owner_id,
                reason=reason,
                expected_revision=expected_revision,
                actor=actor,
                dependencies=dependencies,
            )
        except RequirementError as error:
            _audit_denial(
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.work_item.assign",
                target_type="WORK_ITEM",
                target_id=work_item_id,
                error=error,
            )
            raise
        return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation="requirement_assign_work_item",
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
            action="requirement.work_item.assign",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            error=error,
        )
        raise
    return AssignWorkItemResult.model_validate(execution.response.body)
