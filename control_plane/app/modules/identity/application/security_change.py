from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from control_plane.app.shared.idempotency import IdempotencyClaim


@dataclass(slots=True)
class IdentityChangeSource:
    actor: str
    operation: str
    idempotency_key: str
    source_transaction_id: str
    request_fingerprint: str | None = None
    idempotency_claim_id: str | None = None
    tickets: list[Any] = field(default_factory=list)

    def bind_claim(self, claim: IdempotencyClaim) -> None:
        if not claim.created:
            raise RuntimeError("identity security source must own the idempotency claim")
        self.request_fingerprint = claim.request_fingerprint
        self.idempotency_claim_id = claim.record_id


_source: ContextVar[IdentityChangeSource | None] = ContextVar(
    "identity_change_source",
    default=None,
)


@contextmanager
def identity_change_source(
    *,
    actor: str,
    operation: str,
    idempotency_key: str,
    source_transaction_id: str,
) -> Iterator[IdentityChangeSource]:
    source = IdentityChangeSource(
        actor=actor,
        operation=operation,
        idempotency_key=idempotency_key,
        source_transaction_id=source_transaction_id,
    )
    token: Token[IdentityChangeSource | None] = _source.set(source)
    try:
        yield source
    finally:
        _source.reset(token)


def current_identity_change_source() -> IdentityChangeSource | None:
    return _source.get()


def notify_identity_change(
    callback: Callable[[str], Any],
    account_id: str,
) -> None:
    ticket = callback(account_id)
    source = current_identity_change_source()
    if source is not None and ticket is not None:
        source.tickets.append(ticket)
