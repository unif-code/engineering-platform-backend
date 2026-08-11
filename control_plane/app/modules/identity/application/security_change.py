from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IdentityChangeSource:
    actor: str
    operation: str
    idempotency_key: str
    source_transaction_id: str


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
) -> Iterator[None]:
    token: Token[IdentityChangeSource | None] = _source.set(
        IdentityChangeSource(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            source_transaction_id=source_transaction_id,
        )
    )
    try:
        yield
    finally:
        _source.reset(token)


def current_identity_change_source() -> IdentityChangeSource | None:
    return _source.get()
