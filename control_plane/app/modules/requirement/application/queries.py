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
from control_plane.app.modules.requirement.application.delivery_set import (
    validate_current_delivery_set,
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
    WorkItemAssigneeIneligible,
    WorkItemNotFound,
)
from control_plane.app.modules.requirement.ports import RequirementRepository
from control_plane.app.modules.requirement.ports.runtime import AssignmentGuardPort


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
    for_update: bool = False,
) -> RequirementDetailsDto:
    row = repository.requirement_by_id(requirement_id, for_update=for_update)
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


def assert_work_item_assignee_eligible(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    work_item_id: str,
    expected_assignment_id: str,
    assignment_guard: AssignmentGuardPort,
) -> None:
    """Recheck the selected owner using current facts under the caller's parent lock."""
    requirement = repository.requirement_by_id(requirement_id)
    work_item = repository.work_item_by_id(work_item_id)
    assignment = repository.current_work_item_assignment(work_item_id)
    try:
        if (
            requirement is None
            or work_item is None
            or assignment is None
            or str(requirement["id"]) != requirement_id
            or str(work_item["id"]) != work_item_id
            or str(work_item["requirement_id"]) != requirement_id
            or str(assignment["id"]) != expected_assignment_id
            or str(assignment["work_item_id"]) != work_item_id
            or assignment["superseded_at"] is not None
        ):
            raise ValueError("inconsistent assignment target")
        workspace_id = str(UUID(str(requirement["workspace_id"])))
        owner_id, assignee_id = work_item["human_owner_id"], assignment["assignee_id"]
        repository_id, capabilities = work_item["repository_id"], work_item["required_capabilities"]
        if not isinstance(capabilities, (list, tuple)) or owner_id != assignee_id:
            raise ValueError("inconsistent assignment owner")
        if not all(
            isinstance(value, str) and value and value.strip() == value
            for value in (owner_id, assignee_id, repository_id, *capabilities)
        ):
            raise ValueError("invalid assignment reference")
        explicit_guard = assignment_guard.can_assign
        if not callable(explicit_guard):
            raise ValueError("explicit assignment guard unavailable")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RequirementDependencyUnavailable(
            "Current assignment eligibility is unverifiable"
        ) from None
    eligible = explicit_guard(
        actor_id=assignee_id,
        workspace_id=workspace_id,
        repository_id=repository_id,
        required_capabilities=tuple(capabilities),
    )
    if type(eligible) is not bool:
        raise RequirementDependencyUnavailable("Explicit assignment eligibility is unverifiable")
    if not eligible:
        raise WorkItemAssigneeIneligible("Current WorkItem assignee is not eligible")


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
    validate_current_delivery_set(work_item_ids, row["required_work_item_set_hash"])
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
