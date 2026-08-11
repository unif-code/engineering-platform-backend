from datetime import datetime
from typing import Protocol
from uuid import UUID


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class RandomPort(Protocol):
    def uuid4(self) -> UUID: ...

    def token_urlsafe(self, nbytes: int) -> str: ...

    def totp_secret(self) -> str: ...


class AuthorizationChangePort(Protocol):
    def __call__(self, account_id: str) -> object | None: ...
