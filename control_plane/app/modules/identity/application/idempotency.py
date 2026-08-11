from collections.abc import Callable

from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.ports.repository import IdentityRepository
from control_plane.app.shared.idempotency import (
    CookieReplay,
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
    IdempotentExecution,
    IdempotentResponse,
    SealedIdempotentEnvelope,
    canonical_request_fingerprint,
)
from control_plane.app.shared.idempotency import (
    execute_idempotent as _execute_idempotent,
)


def execute_idempotent(
    repository: IdentityRepository,
    *,
    actor: str,
    operation: str,
    key: str,
    fingerprint: str,
    command: Callable[[], IdempotentResponse],
    dependencies: IdentityDependencies,
) -> IdempotentExecution:
    """Identity-compatible wrapper around the module-neutral command helper."""
    material = dependencies.secret_manager.load()
    return _execute_idempotent(
        repository,
        actor=actor,
        operation=operation,
        key=key,
        fingerprint=fingerprint,
        command=command,
        now=dependencies.clock.now,
        new_id=dependencies.random.uuid4,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )


__all__ = [
    "CookieReplay",
    "IdempotencyConflict",
    "IdempotencyReplayUnavailable",
    "IdempotentExecution",
    "IdempotentResponse",
    "SealedIdempotentEnvelope",
    "canonical_request_fingerprint",
    "execute_idempotent",
]
