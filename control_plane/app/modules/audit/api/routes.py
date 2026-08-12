import base64
import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.audit import list_events
from control_plane.app.modules.audit.adapters.sqlalchemy_repository import (
    SqlAlchemyAuditEventRepository,
)
from control_plane.app.modules.audit.api.dto import (
    AuditEventListResponseDto,
    AuditEventResponseDto,
)
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    problem_response,
)

AUDIT_READ_CAPABILITY = "audit.read"


def _encode_cursor(occurred_at: datetime, event_id: str) -> str:
    value = json.dumps([occurred_at.isoformat(), event_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if cursor is None:
        return None, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(part, str) and part for part in value)
        ):
            raise ValueError
        occurred_at = datetime.fromisoformat(value[0])
        if occurred_at.tzinfo is None:
            raise ValueError
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="Invalid cursor") from None
    return occurred_at, value[1]


def create_audit_router(
    engine_provider: Callable[[], Engine],
    capability_dependency: object,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/audit-events", tags=["audit"])

    @router.get(
        "",
        operation_id="audit_events_list",
        response_model=AuditEventListResponseDto,
        responses={
            **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 422, 500)},
            503: SERVICE_UNAVAILABLE_RESPONSE,
        },
    )
    def audit_events_list(
        principal: Annotated[Any, capability_dependency],
        actor: Annotated[str | None, Query()] = None,
        target_type: Annotated[str | None, Query(alias="targetType")] = None,
        target_id: Annotated[str | None, Query(alias="targetId")] = None,
        from_: Annotated[datetime | None, Query(alias="from")] = None,
        to: Annotated[datetime | None, Query()] = None,
        request_id: Annotated[str | None, Query(alias="requestId")] = None,
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> AuditEventListResponseDto | JSONResponse:
        del principal
        if any(value is not None and value.tzinfo is None for value in (from_, to)):
            raise HTTPException(status_code=422, detail="Time bounds require an offset")
        if from_ is not None and to is not None and from_ >= to:
            raise HTTPException(status_code=422, detail="Invalid time range")
        after_occurred_at, after_id = _decode_cursor(cursor)
        try:
            values = list_events(
                SqlAlchemyAuditEventRepository(engine_provider()),
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                occurred_from=from_,
                occurred_to=to,
                request_id=request_id,
                after_occurred_at=after_occurred_at,
                after_id=after_id,
                limit=limit + 1,
            )
        except SQLAlchemyError:
            return problem_response(503, "Audit unavailable")
        page = values[:limit]
        next_cursor = None
        if len(values) > limit:
            last = page[-1]
            next_cursor = _encode_cursor(last.occurred_at, last.id)
        return AuditEventListResponseDto(
            items=[AuditEventResponseDto.from_domain(value) for value in page],
            next_cursor=next_cursor,
        )

    return router
