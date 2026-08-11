from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class RandomPort(Protocol):
    def uuid4(self) -> object: ...


MembershipChangePort = Callable[[Sequence[str]], None]
