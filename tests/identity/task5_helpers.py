from dataclasses import dataclass
from datetime import UTC, datetime

from control_plane.app.modules.identity import (
    DefaultEffectivePolicy,
    IdentityDependencies,
)
from control_plane.app.modules.identity.adapters.runtime import SystemRandom
from control_plane.app.shared.security import SecretMaterial


@dataclass
class MutableClock:
    value: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class StaticSecrets:
    def load(self) -> SecretMaterial:
        return SecretMaterial(b"p" * 32, b"t" * 32, b"i" * 32)


def dependencies(*, clock: MutableClock | None = None) -> IdentityDependencies:
    return IdentityDependencies(
        secret_manager=StaticSecrets(),
        policy=DefaultEffectivePolicy(),
        clock=clock or MutableClock(),
        random=SystemRandom(),
    )
