from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class RandomPort(Protocol):
    def uuid4(self) -> UUID: ...


class IdentitySessionPort(Protocol):
    def validate(self, raw_token: str) -> Any | None: ...


class WorkspaceMembershipPort(Protocol):
    def is_formal_member(self, workspace_id: str, account_id: str) -> bool: ...
