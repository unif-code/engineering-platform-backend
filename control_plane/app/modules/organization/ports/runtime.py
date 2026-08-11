from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class RandomPort(Protocol):
    def uuid4(self) -> object: ...


MembershipChangePort = Callable[[Sequence[str]], None]


class SecurityChangePort(Protocol):
    def begin(self, *, reason: str, account_ids: Sequence[str] | None = None) -> Any: ...

    def complete(
        self,
        ticket: Any,
        *,
        affected_account_ids: Sequence[str] = (),
        recompute_membership: bool = False,
    ) -> set[str]: ...

    def cancel(self, ticket: Any) -> set[str]: ...
