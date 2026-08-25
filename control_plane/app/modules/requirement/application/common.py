from typing import Any

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    AssignmentState,
    ExecutorType,
    RecordState,
    RepositoryState,
    RequirementDto,
    RequirementState,
    RequirementType,
    WorkItemDto,
    WorkItemState,
)
from control_plane.app.modules.requirement.ports import RequirementRepository
from control_plane.app.shared.api.request_id import current_request_id


def actor_id(actor: Any) -> str:
    value = getattr(actor, "account_id", None) or getattr(actor, "employee_id", None)
    if not isinstance(value, str) or not value:
        raise ValueError("Requirement actor requires a stable identifier")
    return value


def requirement_dto(row: Any) -> RequirementDto:
    return RequirementDto(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        type=RequirementType(row["type"]),
        title=row["title"],
        description=row["description"],
        acceptance_criteria=tuple(row["acceptance_criteria"]),
        created_by=row["created_by"],
        initial_repository_id=row["initial_repository_id"],
        route_snapshot_version=row["route_snapshot_version"],
        route_snapshot_hash=row["route_snapshot_hash"],
        state=RequirementState(row["state"]),
        record_state=RecordState(row["record_state"]),
        requirement_version=row["requirement_version"],
        required_work_item_set_version=row["required_work_item_set_version"],
        required_work_item_set_hash=row["required_work_item_set_hash"],
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def work_item_dto(row: Any) -> WorkItemDto:
    return WorkItemDto(
        id=str(row["id"]),
        requirement_id=str(row["requirement_id"]),
        created_by=row["created_by"],
        human_owner_id=row["human_owner_id"],
        executor_type=ExecutorType(row["executor_type"]),
        executor_id=row["executor_id"],
        required_capabilities=tuple(row["required_capabilities"]),
        assignment_state=AssignmentState(row["assignment_state"]),
        repository_state=RepositoryState(row["repository_state"]),
        state=WorkItemState(row["state"]),
        repository_id=row["repository_id"],
        base_commit_sha=row["base_commit_sha"],
        task_branch=row["task_branch"],
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def audit(
    repository: RequirementRepository,
    *,
    dependencies: RequirementDependencies,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
) -> None:
    now = dependencies.clock.now()
    record_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=now,
            actor=actor,
            actor_type="SYSTEM" if actor == "SYSTEM" else "HUMAN",
            action=action,
            target_type=target_type,
            target_id=target_id,
            result="SUCCESS",
            reason=reason,
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.audit,
    )
