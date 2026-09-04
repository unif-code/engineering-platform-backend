import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentAuditAppend,
    AttemptMutation,
    AttemptState,
    CanonicalEventInput,
    CheckpointInput,
    EventAcceptanceReceipt,
    EventReplayConflict,
    RunMutation,
    RunState,
    transition_attempt,
)
from control_plane.app.modules.agent.ports import AgentUnitOfWork

_EVENT_TARGETS: Final[dict[str, AttemptState]] = {
    "ATTEMPT_PROVISIONING": AttemptState.PROVISIONING,
    "ATTEMPT_RUNNING": AttemptState.RUNNING,
    "WAITING_INPUT": AttemptState.WAITING_INPUT,
    "ATTEMPT_FINALIZING": AttemptState.FINALIZING,
    "ATTEMPT_SUCCEEDED": AttemptState.SUCCEEDED,
    "ATTEMPT_FAILED": AttemptState.FAILED,
    "ATTEMPT_CANCELED": AttemptState.CANCELED,
    "ATTEMPT_TIMED_OUT": AttemptState.TIMED_OUT,
}
_TERMINAL_RUN_STATES: Final[dict[AttemptState, RunState]] = {
    AttemptState.SUCCEEDED: RunState.SUCCEEDED,
    AttemptState.FAILED: RunState.FAILED,
    AttemptState.CANCELED: RunState.CANCELED,
    AttemptState.TIMED_OUT: RunState.TIMED_OUT,
}


class StaleRunnerGeneration(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _StaleGenerationRejection:
    attempt_id: str


class EventSequenceGap(ValueError):
    pass


class EventReplayUnavailable(RuntimeError):
    """Predecessor evidence has no trustworthy original acceptance snapshot."""


class UnknownWorkflowEvent(ValueError):
    pass


class EventAcceptance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event: CanonicalEventInput
    attempt: AgentAttempt
    checkpoint: CheckpointInput | None


def _canonical_digest(event: CanonicalEventInput) -> str:
    payload = json.dumps(
        event.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _audit_reference(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _checkpoint(event: CanonicalEventInput) -> tuple[CheckpointInput, datetime]:
    raw_checkpoint = event.data.get("checkpoint")
    raw_deadline = event.data.get("waitingDeadline")
    if not isinstance(raw_checkpoint, Mapping) or not isinstance(raw_deadline, str):
        raise ValueError("WAITING_INPUT requires platform checkpoint and waitingDeadline")
    checkpoint = CheckpointInput.model_validate(raw_checkpoint)
    deadline = datetime.fromisoformat(raw_deadline)
    if deadline.tzinfo is None:
        raise ValueError("waitingDeadline must include an offset")
    return checkpoint, deadline


def _mutation(
    attempt: AgentAttempt,
    *,
    checkpoint: CheckpointInput | None,
    waiting_deadline: datetime | None,
    now: datetime,
) -> AttemptMutation:
    return AttemptMutation(
        state=attempt.state,
        runner_generation=attempt.runner_generation,
        fencing_token=attempt.fencing_token,
        checkpoint=attempt.checkpoint,
        event_sequence=attempt.event_sequence,
        waiting_deadline=waiting_deadline,
        terminal_evidence=attempt.terminal_evidence,
        now=now,
    )


def accept_workflow_event(
    event: CanonicalEventInput, *, dependencies: AgentDependencies
) -> EventAcceptance:
    """Accept platform canonical worker evidence under the Attempt row lock."""

    # Do not trust a caller's model_construct or custom copy implementation.
    event = CanonicalEventInput.model_validate(event.model_dump(mode="python"))
    actor = dependencies.actor_resolver.resolve("system-901")

    def operation(uow: AgentUnitOfWork) -> EventAcceptance | _StaleGenerationRejection:
        repository = uow.repository()
        existing_before_lock = repository.event_by_id(event.id)
        lock_attempt_id = (
            existing_before_lock.attempt_id
            if existing_before_lock is not None
            else event.attempt_id
        )
        unlocked_attempt = repository.attempt_by_id(lock_attempt_id)
        if unlocked_attempt is None:
            raise ValueError("canonical event references an unknown Attempt")
        run = repository.run_by_id(unlocked_attempt.run_id, for_update=True)
        if run is None:
            raise ValueError("Attempt references an unknown Agent Run")
        attempt = repository.attempt_by_id(lock_attempt_id, for_update=True)
        if attempt is None or attempt.run_id != run.id:
            raise ValueError("canonical event Attempt identity changed")
        existing = repository.event_by_id(event.id)
        if existing is not None:
            if _canonical_digest(existing) != _canonical_digest(event):
                raise EventReplayConflict(f"canonical event {event.id} changed during replay")
            receipt = repository.event_receipt_by_id(event.id)
            if receipt is None:
                raise EventReplayUnavailable("original event acceptance is unavailable")
            return EventAcceptance(
                event=existing,
                attempt=receipt.attempt,
                checkpoint=receipt.checkpoint,
            )
        if event.attempt_id != attempt.id:
            raise ValueError("canonical event references an unknown Attempt")
        now = dependencies.clock()
        if event.generation != attempt.runner_generation:
            uow.append_audit_event(
                AgentAuditAppend(
                    id=dependencies.new_id(),
                    occurred_at=now,
                    actor=actor.reference,
                    actor_type=actor.actor_type,
                    action="agent.event.reject_stale_generation",
                    target_type="agent_attempt",
                    target_id=attempt.id,
                    result="REJECTED",
                    reason=(
                        "STALE_RUNNER_GENERATION;"
                        f"generation={event.generation};currentGeneration={attempt.runner_generation};"
                        f"eventRef={_audit_reference(event.id)}"
                    ),
                    correlation_id=f"agent-correlation:{_audit_reference(event.correlation_id)}",
                    schema_version=1,
                )
            )
            return _StaleGenerationRejection(attempt.id)
        expected_sequence = attempt.event_sequence + 1
        if event.sequence != expected_sequence:
            raise EventSequenceGap(
                f"expected event sequence {expected_sequence}, received {event.sequence}"
            )
        target = _EVENT_TARGETS.get(event.event_type)
        if target is None:
            raise UnknownWorkflowEvent(event.event_type)

        checkpoint: CheckpointInput | None = None
        waiting_deadline: datetime | None = attempt.waiting_deadline
        if target is AttemptState.WAITING_INPUT:
            checkpoint, waiting_deadline = _checkpoint(event)
            checkpoint = repository.insert_checkpoint(attempt.id, checkpoint)
        transitioned = transition_attempt(attempt, target, checkpoint=checkpoint, now=now)
        mutation = _mutation(
            transitioned,
            checkpoint=checkpoint,
            waiting_deadline=waiting_deadline,
            now=now,
        ).model_copy(update={"event_sequence": event.sequence})
        persisted_attempt = repository.compare_and_set_attempt(
            attempt.id, expected_revision=attempt.revision, mutation=mutation
        )
        if persisted_attempt is None:
            raise RuntimeError("Attempt mutation lost its row lock")
        persisted_event = repository.append_event(event)
        repository.append_event_receipt(
            EventAcceptanceReceipt(
                event_id=persisted_event.id,
                attempt=persisted_attempt,
                checkpoint=checkpoint,
            )
        )
        run_state = _TERMINAL_RUN_STATES.get(target)
        if run_state is not None:
            if (
                repository.compare_and_set_run(
                    run.id,
                    expected_revision=run.revision,
                    mutation=RunMutation(
                        state=run_state, latest_attempt_id=run.latest_attempt_id, now=now
                    ),
                )
                is None
            ):
                raise RuntimeError("Run mutation lost its row lock")
        uow.append_audit_event(
            AgentAuditAppend(
                id=dependencies.new_id(),
                occurred_at=now,
                actor=actor.reference,
                actor_type=actor.actor_type,
                action={
                    AttemptState.WAITING_INPUT: "agent.attempt.waiting_input",
                    AttemptState.SUCCEEDED: "agent.attempt.succeeded",
                }.get(target, "agent.event.accept"),
                target_type="agent_attempt",
                target_id=attempt.id,
                result=target.value,
                reason=(
                    f"eventType={event.event_type};generation={event.generation};"
                    f"sequence={event.sequence};eventRef={_audit_reference(event.id)}"
                ),
                correlation_id=f"agent-correlation:{_audit_reference(event.correlation_id)}",
                schema_version=1,
            )
        )
        return EventAcceptance(
            event=persisted_event,
            attempt=persisted_attempt,
            checkpoint=checkpoint,
        )

    result = dependencies.transaction_runner(operation)
    if isinstance(result, _StaleGenerationRejection):
        # The owned transaction has committed rejection evidence, but no Agent facts.
        raise StaleRunnerGeneration(result.attempt_id)
    return result
