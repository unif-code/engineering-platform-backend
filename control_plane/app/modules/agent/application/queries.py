from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentRun,
    CanonicalEventInput,
    ExecutionBinding,
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


class InvalidEventCursor(ValueError):
    pass


class InvalidEventPageLimit(ValueError):
    pass


class AgentRunNotFound(LookupError):
    pass


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
    if limit < 1 or limit > 100:
        raise InvalidEventPageLimit("event page limit must be between 1 and 100")

    def operation(uow: AgentUnitOfWork) -> CanonicalEventPage:
        repository = uow.repository()
        if repository.run_by_id(run_id) is None:
            raise AgentRunNotFound(run_id)
        events = repository.events_by_run_id(run_id)
        start = 0
        if cursor is not None:
            try:
                event_id = dependencies.cursor_codec.decode(run_id=run_id, cursor=cursor)
            except ValueError as error:
                raise InvalidEventCursor("invalid canonical event cursor") from error
            index = next((i for i, item in enumerate(events) if item.id == event_id), None)
            if index is None:
                raise InvalidEventCursor("event cursor does not belong to Agent Run")
            start = index + 1
        page = events[start : start + limit]
        next_cursor = (
            dependencies.cursor_codec.encode(run_id=run_id, event_id=page[-1].id)
            if start + len(page) < len(events) and page
            else None
        )
        return CanonicalEventPage(items=page, next_cursor=next_cursor)

    return dependencies.transaction_runner(operation)
