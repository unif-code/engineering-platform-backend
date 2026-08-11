import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pyotp


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SystemRandom:
    def uuid4(self) -> UUID:
        return uuid4()

    def token_urlsafe(self, nbytes: int) -> str:
        return secrets.token_urlsafe(nbytes)

    def totp_secret(self) -> str:
        return pyotp.random_base32()


def no_auth_change(account_id: str) -> None:
    del account_id
