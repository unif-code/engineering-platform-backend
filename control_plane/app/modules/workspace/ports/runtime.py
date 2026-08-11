from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class RandomPort(Protocol):
    def uuid4(self) -> object: ...


class SecurityChangePort(Protocol):
    def begin(
        self,
        *,
        reason: str,
        source_module: str,
        actor: str,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str | None = None,
        idempotency_claim_id: str | None = None,
        source_transaction_id: str | None = None,
        account_ids: Sequence[str] | None = None,
        affected_account_ids: Sequence[str] = (),
        affected_workspace_ids: Sequence[str] = (),
        recompute_membership: bool = False,
    ) -> Any: ...

    def complete(
        self,
        ticket: Any,
        *,
        affected_account_ids: Sequence[str] = (),
        recompute_membership: bool = False,
    ) -> set[str]: ...

    def cancel(self, ticket: Any) -> set[str]: ...

    def claim_converged(self, source_module: str, idempotency_claim_id: str) -> bool: ...
