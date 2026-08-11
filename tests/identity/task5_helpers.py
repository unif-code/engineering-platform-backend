from dataclasses import dataclass
from datetime import UTC, datetime

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.identity import IdentityDependencies
from control_plane.app.modules.identity.adapters.policy import DefaultEffectivePolicy
from control_plane.app.modules.identity.adapters.runtime import SystemRandom
from control_plane.app.modules.identity.adapters.sqlalchemy import SqlAlchemyIdentityRepository
from control_plane.app.shared.security import SecretMaterial


@dataclass
class MutableClock:
    value: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class StaticSecrets:
    def load(self) -> SecretMaterial:
        return SecretMaterial(b"p" * 32, b"t" * 32, b"i" * 32)


def no_auth_change(account_id: str) -> None:
    del account_id


def dependencies(*, clock: MutableClock | None = None) -> IdentityDependencies:
    return IdentityDependencies(
        repository_factory=SqlAlchemyIdentityRepository,
        secret_manager=StaticSecrets(),
        policy=DefaultEffectivePolicy(),
        clock=clock or MutableClock(),
        random=SystemRandom(),
        audit=SqlAlchemyTransactionalAuditAppender(),
        on_auth_change=no_auth_change,
    )
