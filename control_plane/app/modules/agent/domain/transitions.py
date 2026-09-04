from datetime import datetime

from control_plane.app.modules.agent.domain.errors import (
    AttemptNotResumable,
    CheckpointReplacementForbidden,
    CheckpointRequired,
    IllegalAttemptTransition,
    InvalidFencingToken,
    ResumeGenerationRequired,
)
from control_plane.app.modules.agent.domain.models import (
    AgentAttempt,
    AttemptState,
    CheckpointInput,
)

ALLOWED_TRANSITIONS: dict[AttemptState, set[AttemptState]] = {
    AttemptState.CREATED: {AttemptState.BINDING, AttemptState.CANCELING},
    AttemptState.BINDING: {
        AttemptState.QUEUED,
        AttemptState.FAILED,
        AttemptState.CANCELING,
    },
    AttemptState.QUEUED: {AttemptState.PROVISIONING, AttemptState.CANCELING},
    AttemptState.PROVISIONING: {
        AttemptState.RUNNING,
        AttemptState.FAILED,
        AttemptState.CANCELING,
    },
    AttemptState.RUNNING: {
        AttemptState.WAITING_INPUT,
        AttemptState.FINALIZING,
        AttemptState.CANCELING,
    },
    AttemptState.WAITING_INPUT: {AttemptState.QUEUED, AttemptState.CANCELING},
    AttemptState.FINALIZING: {AttemptState.SUCCEEDED, AttemptState.FAILED},
    AttemptState.CANCELING: {AttemptState.CANCELED, AttemptState.TIMED_OUT},
    AttemptState.SUCCEEDED: set(),
    AttemptState.FAILED: set(),
    AttemptState.CANCELED: set(),
    AttemptState.TIMED_OUT: set(),
}


def transition_attempt(
    attempt: AgentAttempt,
    target: AttemptState,
    *,
    checkpoint: CheckpointInput | None,
    now: datetime,
) -> AgentAttempt:
    if target not in ALLOWED_TRANSITIONS[attempt.state]:
        raise IllegalAttemptTransition(f"{attempt.state.value}->{target.value}")
    if attempt.state is AttemptState.WAITING_INPUT and target is AttemptState.QUEUED:
        raise ResumeGenerationRequired(attempt.id)
    if target is AttemptState.WAITING_INPUT and checkpoint is None:
        raise CheckpointRequired(attempt.id)
    if target is not AttemptState.WAITING_INPUT and checkpoint is not None:
        raise CheckpointReplacementForbidden(attempt.id)

    return attempt.model_copy(
        update={
            "state": target,
            "checkpoint": checkpoint
            if target is AttemptState.WAITING_INPUT
            else attempt.checkpoint,
            "revision": attempt.revision + 1,
            "updated_at": now,
        }
    )


def resume_generation(
    attempt: AgentAttempt,
    *,
    fencing_token: str,
    now: datetime,
) -> AgentAttempt:
    if attempt.state is not AttemptState.WAITING_INPUT or attempt.checkpoint is None:
        raise AttemptNotResumable(attempt.id)
    if not fencing_token.strip() or fencing_token == attempt.fencing_token:
        raise InvalidFencingToken(attempt.id)

    return attempt.model_copy(
        update={
            "state": AttemptState.QUEUED,
            "runner_generation": attempt.runner_generation + 1,
            "fencing_token": fencing_token,
            "revision": attempt.revision + 1,
            "updated_at": now,
        }
    )
