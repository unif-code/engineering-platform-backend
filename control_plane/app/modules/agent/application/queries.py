import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentRun,
    AgentRunListItem,
    CanonicalEventInput,
    EventCursorAnchorMissing,
    ExecutionBinding,
    RunState,
)
from control_plane.app.modules.agent.ports import AgentUnitOfWork


class AgentRunView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: AgentRun
    attempts: tuple[AgentAttempt, ...]
    bindings: tuple[ExecutionBinding, ...]


class CanonicalEventPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[CanonicalEventInput, ...]
    next_cursor: str | None


class AgentRunPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[AgentRunListItem, ...]
    next_cursor: str | None


class InvalidRunCursor(ValueError):
    pass


class InvalidRunPageLimit(ValueError):
    pass


class InvalidEventCursor(ValueError):
    pass


class InvalidEventPageLimit(ValueError):
    pass


class AgentRunNotFound(LookupError):
    pass


def get_run_metadata(run_id: str, *, dependencies: AgentDependencies) -> AgentRun:
    def operation(uow: AgentUnitOfWork) -> AgentRun:
        run = uow.repository().run_by_id(run_id)
        if run is None:
            raise AgentRunNotFound(run_id)
        return run

    return dependencies.transaction_runner(operation)


def list_runs(
    workspace_id: str,
    *,
    state: RunState | None,
    cursor: str | None,
    limit: int = 50,
    dependencies: AgentDependencies,
) -> AgentRunPage:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise InvalidRunPageLimit("Run page limit must be between 1 and 100")
    before_at = None
    before_id = None
    if cursor is not None:
        try:
            values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (
                not isinstance(values, list)
                or len(values) != 5
                or type(values[0]) is not int
                or values[:3] != [1, workspace_id, state]
                or not isinstance(values[3], str)
                or not isinstance(values[4], str)
            ):
                raise ValueError
            before_at = datetime.fromisoformat(values[3])
            if before_at.tzinfo is None:
                raise ValueError
            before_at = before_at.astimezone(UTC)
            before_id = str(UUID(values[4]))
        except (ValueError, TypeError, OverflowError, binascii.Error):
            raise InvalidRunCursor("invalid Workspace/state Run cursor") from None

    def operation(uow: AgentUnitOfWork) -> AgentRunPage:
        rows = uow.repository().runs_page(
            workspace_id, state=state, before_at=before_at, before_id=before_id, limit=limit + 1
        )
        items = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1].run
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    [1, workspace_id, state, last.created_at.isoformat(), last.id],
                    separators=(",", ":"),
                ).encode()
            ).decode()
        return AgentRunPage(items=items, next_cursor=next_cursor)

    return dependencies.transaction_runner(operation)


def get_run(run_id: str, *, dependencies: AgentDependencies) -> AgentRunView:
    def operation(uow: AgentUnitOfWork) -> AgentRunView:
        repository = uow.repository()
        run = repository.run_by_id(run_id)
        if run is None:
            raise AgentRunNotFound(run_id)
        attempts = repository.attempts_by_run_id(run.id)
        bindings = tuple(
            binding
            for attempt in attempts
            if (binding := repository.binding_by_attempt_id(attempt.id)) is not None
        )
        return AgentRunView(run=run, attempts=attempts, bindings=bindings)

    return dependencies.transaction_runner(operation)


def list_events(
    run_id: str,
    *,
    cursor: str | None,
    limit: int = 50,
    dependencies: AgentDependencies,
) -> CanonicalEventPage:
    if type(limit) is not int or limit < 1 or limit > 100:
        raise InvalidEventPageLimit("event page limit must be between 1 and 100")

    def operation(uow: AgentUnitOfWork) -> CanonicalEventPage:
        repository = uow.repository()
        if repository.run_by_id(run_id) is None:
            raise AgentRunNotFound(run_id)
        event_id = None
        if cursor is not None:
            try:
                event_id = str(UUID(dependencies.cursor_codec.decode(run_id=run_id, cursor=cursor)))
            except ValueError as error:
                raise InvalidEventCursor("invalid canonical event cursor") from error
        try:
            events = repository.events_page_by_run_id(
                run_id, after_event_id=event_id, limit=limit + 1
            )
        except EventCursorAnchorMissing:
            raise InvalidEventCursor("event cursor does not belong to Agent Run") from None
        page = events[:limit]
        next_cursor = (
            dependencies.cursor_codec.encode(run_id=run_id, event_id=page[-1].id)
            if len(events) > limit and page
            else None
        )
        return CanonicalEventPage(items=page, next_cursor=next_cursor)

    return dependencies.transaction_runner(operation)
