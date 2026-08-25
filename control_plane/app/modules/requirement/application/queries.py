import base64
import json
from datetime import datetime
from uuid import UUID

from control_plane.app.modules.requirement.application.common import (
    requirement_dto,
    work_item_dto,
)
from control_plane.app.modules.requirement.domain import (
    InvalidRequirementCursor,
    RequirementDetailsDto,
    RequirementNotFound,
    RequirementPage,
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
    return RequirementDetailsDto(
        requirement=requirement_dto(row),
        work_items=tuple(work_item_dto(item) for item in repository.work_items(requirement_id)),
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
