import base64
import json
from datetime import datetime
from uuid import UUID

from control_plane.app.modules.requirement.application.common import (
    decision_dto,
    gate_assignment_dto,
    gate_instance_dto,
    requirement_dto,
    sdd_baseline_dto,
    work_item_assignment_dto,
    work_item_dto,
)
from control_plane.app.modules.requirement.domain import (
    AssignmentState,
    InvalidRequirementCursor,
    RepositoryBindingContext,
    RequirementDeliverySnapshotDto,
    RequirementDependencyUnavailable,
    RequirementDetailsDto,
    RequirementNotFound,
    RequirementPage,
    RequirementType,
    WorkItemNotFound,
    required_work_item_set_hash,
)
from control_plane.app.modules.requirement.ports import RequirementRepository


def _encode_cursor(created_at: datetime, requirement_id: str) -> str:
    payload = json.dumps(
        {"createdAt": created_at.isoformat(), "id": requirement_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if cursor is None:
        return None, None
    try:
        payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != {"createdAt", "id"}:
            raise ValueError
        created_at = datetime.fromisoformat(payload["createdAt"])
        requirement_id = str(UUID(payload["id"]))
        if created_at.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise InvalidRequirementCursor("invalid Requirement cursor") from None
    return created_at, requirement_id


def get_requirement(
    repository: RequirementRepository,
    *,
    requirement_id: str,
) -> RequirementDetailsDto:
    row = repository.requirement_by_id(requirement_id)
    if row is None:
        raise RequirementNotFound(requirement_id)
    baseline = (
        None
        if row["current_sdd_baseline_id"] is None
        else repository.sdd_baseline_by_id(str(row["current_sdd_baseline_id"]))
    )
    gate = None if baseline is None else repository.gate_by_baseline_id(str(baseline["id"]))
    gate_assignment = None if gate is None else repository.current_gate_assignment(str(gate["id"]))
    decision = None if gate is None else repository.decision_by_gate_id(str(gate["id"]))
    return RequirementDetailsDto(
        requirement=requirement_dto(row),
        work_items=tuple(work_item_dto(item) for item in repository.work_items(requirement_id)),
        work_item_assignments=tuple(
            work_item_assignment_dto(item)
            for item in repository.current_work_item_assignments(requirement_id)
        ),
        current_sdd_baseline=None if baseline is None else sdd_baseline_dto(baseline),
        current_gate=None if gate is None else gate_instance_dto(gate),
        current_gate_assignment=(
            None if gate_assignment is None else gate_assignment_dto(gate_assignment)
        ),
        current_decision=None if decision is None else decision_dto(decision),
    )


def get_requirement_delivery_snapshot(
    repository: RequirementRepository,
    *,
    requirement_id: str,
) -> RequirementDeliverySnapshotDto:
    """Read the current versioned delivery input without creating a freeze fact."""
    row = repository.requirement_delivery_snapshot(requirement_id)
    if row is None:
        raise RequirementNotFound(requirement_id)
    work_item_ids = tuple(str(work_item_id) for work_item_id in row["work_item_ids"])
    if required_work_item_set_hash(work_item_ids) != row["required_work_item_set_hash"]:
        raise RequirementDependencyUnavailable("Requirement delivery snapshot is inconsistent")
    return RequirementDeliverySnapshotDto(
        requirement_id=str(row["id"]),
        requirement_version=row["requirement_version"],
        required_work_item_set_version=row["required_work_item_set_version"],
        required_work_item_set_hash=row["required_work_item_set_hash"],
        work_item_ids=work_item_ids,
    )


def list_requirements(
    repository: RequirementRepository,
    *,
    workspace_id: str,
    cursor: str | None,
    limit: int,
) -> RequirementPage:
    if not 1 <= limit <= 100:
        raise InvalidRequirementCursor("Requirement page limit must be between 1 and 100")
    after_created_at, after_id = _decode_cursor(cursor)
    rows = repository.list_requirements(
        workspace_id=workspace_id,
        after_created_at=after_created_at,
        after_id=after_id,
        limit=limit + 1,
    )
    visible = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = visible[-1]
        next_cursor = _encode_cursor(last["created_at"], str(last["id"]))
    return RequirementPage(
        items=tuple(requirement_dto(row) for row in visible),
        next_cursor=next_cursor,
    )


def get_repository_binding_context(
    repository: RequirementRepository,
    *,
    work_item_id: str,
) -> RepositoryBindingContext:
    row = repository.repository_binding_context(work_item_id)
    if row is None:
        raise WorkItemNotFound(work_item_id)
    return RepositoryBindingContext(
        requirement_id=str(row["requirement_id"]),
        requirement_type=RequirementType(row["requirement_type"]),
        requirement_title=row["requirement_title"],
        workspace_id=str(row["workspace_id"]),
        work_item_id=str(row["work_item_id"]),
        work_item_revision=row["work_item_revision"],
        repository_id=row["repository_id"],
        assignment_state=AssignmentState(row["assignment_state"]),
        human_owner_id=row["human_owner_id"],
        required_capabilities=tuple(row["required_capabilities"]),
    )
