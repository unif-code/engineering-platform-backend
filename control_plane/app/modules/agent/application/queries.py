import base64
import binascii
import json
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.errors import (
    AgentBusinessContextReason,
    InvalidRequirementExecutionContext,
)
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentQueryUnavailable,
    AgentRun,
    AgentRunBusinessContext,
    AgentRunListItem,
    AttemptState,
    CanonicalEventInput,
    CheckpointInput,
    EventCursorAnchorMissing,
    ExecutionBinding,
    RunState,
    WaitingInputQuestion,
)
from control_plane.app.modules.agent.domain.types import PlatformUUID
from control_plane.app.modules.agent.ports import AgentRepository, AgentUnitOfWork
from control_plane.app.modules.agent.ports.runtime import (
    RequirementExecutionContext,
    RequirementExecutionRequest,
)
from control_plane.app.modules.requirement import (
    RequirementDependencyUnavailable,
    RequirementNotFound,
)


class AgentWaitingInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: PlatformUUID
    attempt_id: PlatformUUID
    generation: int = Field(ge=1, strict=True)
    checkpoint_id: PlatformUUID
    waiting_deadline: AwareDatetime
    question: WaitingInputQuestion


class AgentRunView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: AgentRun
    attempts: tuple[AgentAttempt, ...]
    bindings: tuple[ExecutionBinding, ...]
    waiting_input: AgentWaitingInput | None


class CanonicalEventPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[CanonicalEventInput, ...]
    next_cursor: str | None


class AgentRunPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[AgentRunListItem, ...]
    next_cursor: str | None


class AgentBusinessContextCurrentness(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNVERIFIABLE = "UNVERIFIABLE"


class AgentRunBusinessContextStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: PlatformUUID
    workspace_id: PlatformUUID
    checked_at: AwareDatetime
    currentness: AgentBusinessContextCurrentness
    reasons: tuple[AgentBusinessContextReason, ...]

    @model_validator(mode="after")
    def current_requires_no_reasons(self) -> "AgentRunBusinessContextStatus":
        if (self.currentness is AgentBusinessContextCurrentness.CURRENT) != (not self.reasons):
            raise ValueError("Only CURRENT has no reasons")
        return self


def get_business_context_status(
    run: AgentRun, *, dependencies: AgentDependencies
) -> AgentRunBusinessContextStatus:
    def observed(
        currentness: AgentBusinessContextCurrentness,
        reason: AgentBusinessContextReason | None = None,
    ) -> AgentRunBusinessContextStatus:
        return AgentRunBusinessContextStatus(
            run_id=run.id,
            workspace_id=run.workspace_id,
            checked_at=dependencies.clock(),
            currentness=currentness,
            reasons=() if reason is None else (reason,),
        )

    source = run.business_context
    if source is None:
        return observed(
            AgentBusinessContextCurrentness.UNVERIFIABLE,
            AgentBusinessContextReason.BUSINESS_CONTEXT_NOT_RECORDED,
        )
    try:
        current = dependencies.requirement_context.resolve(
            RequirementExecutionRequest(
                workspace_id=run.workspace_id,
                requirement_id=source.requirement_id,
                work_item_id=source.work_item_id,
            )
        )
    except RequirementNotFound:
        return observed(
            AgentBusinessContextCurrentness.STALE, AgentBusinessContextReason.REQUIREMENT_NOT_FOUND
        )
    except InvalidRequirementExecutionContext as error:
        reason = error.reason
        if reason in (
            AgentBusinessContextReason.WORKSPACE_CHANGED,
            AgentBusinessContextReason.WORK_ITEM_NOT_IN_REQUIREMENT,
            AgentBusinessContextReason.ASSIGNMENT_MISSING,
        ):
            return observed(AgentBusinessContextCurrentness.STALE, reason)
        return observed(
            AgentBusinessContextCurrentness.UNVERIFIABLE,
            AgentBusinessContextReason.OWNER_DATA_AMBIGUOUS
            if reason is AgentBusinessContextReason.OWNER_DATA_AMBIGUOUS
            else AgentBusinessContextReason.OWNER_DATA_INVALID,
        )
    except RequirementDependencyUnavailable:
        return observed(
            AgentBusinessContextCurrentness.UNVERIFIABLE,
            AgentBusinessContextReason.OWNER_UNAVAILABLE,
        )
    try:
        current = RequirementExecutionContext.model_validate(current.model_dump(mode="python"))
        workspace_id = str(UUID(current.workspace_id))
        identities = AgentRunBusinessContext(
            requirement_id=current.requirement_id,
            work_item_id=current.work_item_id,
            assignment_id=current.assignment_id,
        )
    except (ValueError, TypeError, AttributeError):
        return observed(
            AgentBusinessContextCurrentness.UNVERIFIABLE,
            AgentBusinessContextReason.OWNER_DATA_INVALID,
        )
    if (
        identities.requirement_id != source.requirement_id
        or identities.work_item_id != source.work_item_id
    ):
        return observed(
            AgentBusinessContextCurrentness.UNVERIFIABLE,
            AgentBusinessContextReason.OWNER_DATA_INVALID,
        )
    if workspace_id != run.workspace_id:
        return observed(
            AgentBusinessContextCurrentness.STALE, AgentBusinessContextReason.WORKSPACE_CHANGED
        )
    if identities.assignment_id != source.assignment_id:
        return observed(
            AgentBusinessContextCurrentness.STALE, AgentBusinessContextReason.ASSIGNMENT_CHANGED
        )
    return observed(AgentBusinessContextCurrentness.CURRENT)


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


def _waiting_input(
    repository: AgentRepository, run: AgentRun, attempt: AgentAttempt
) -> AgentWaitingInput | None:
    if attempt.run_id != run.id:
        raise AgentQueryUnavailable("Latest Attempt does not belong to Run")
    if attempt.state is not AttemptState.WAITING_INPUT:
        return None
    try:
        event = repository.event_by_position(
            attempt.id, generation=attempt.runner_generation, sequence=attempt.event_sequence
        )
        if event is None:
            raise ValueError("missing waiting event")
        event = CanonicalEventInput.model_validate(event.model_dump(mode="python"))
        if (
            event.event_type != "WAITING_INPUT"
            or event.attempt_id != attempt.id
            or event.generation != attempt.runner_generation
            or event.sequence != attempt.event_sequence
            or CheckpointInput.model_validate(event.data["checkpoint"]) != attempt.checkpoint
            or datetime.fromisoformat(str(event.data["waitingDeadline"]))
            != attempt.waiting_deadline
        ):
            raise ValueError("waiting event contradicts current Attempt snapshot")
        if "question" not in event.data:
            return None
        assert attempt.checkpoint is not None and attempt.waiting_deadline is not None
        return AgentWaitingInput(
            event_id=event.id,
            attempt_id=attempt.id,
            generation=attempt.runner_generation,
            checkpoint_id=attempt.checkpoint.id,
            waiting_deadline=attempt.waiting_deadline,
            question=WaitingInputQuestion.model_validate(event.data["question"]),
        )
    except (ValueError, TypeError, AttributeError, KeyError):
        raise AgentQueryUnavailable("Current waiting input evidence is unavailable") from None


def get_run(run_id: str, *, dependencies: AgentDependencies) -> AgentRunView:
    def operation(uow: AgentUnitOfWork) -> AgentRunView:
        repository = uow.repository()
        run = repository.run_by_id(run_id)
        if run is None:
            raise AgentRunNotFound(run_id)
        attempts = repository.attempts_by_run_id(run.id)
        current = next(
            (attempt for attempt in attempts if attempt.id == run.latest_attempt_id), None
        )
        if current is None:
            raise AgentQueryUnavailable("Latest Attempt is unavailable")
        bindings = tuple(
            binding
            for attempt in attempts
            if (binding := repository.binding_by_attempt_id(attempt.id)) is not None
        )
        return AgentRunView(
            run=run,
            attempts=attempts,
            bindings=bindings,
            waiting_input=_waiting_input(repository, run, current),
        )

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
