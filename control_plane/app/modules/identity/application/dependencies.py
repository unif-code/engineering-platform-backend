from dataclasses import dataclass

from control_plane.app.modules.identity.ports.policy import EffectivePolicyPort
from control_plane.app.modules.identity.ports.runtime import (
    AuthorizationChangePort,
    ClockPort,
    RandomPort,
)
from control_plane.app.shared.security import SecretManagerPort


def _no_auth_change(account_id: str) -> None:
    del account_id


@dataclass(frozen=True, slots=True)
class IdentityDependencies:
    secret_manager: SecretManagerPort
    policy: EffectivePolicyPort
    clock: ClockPort
    random: RandomPort
    on_auth_change: AuthorizationChangePort = _no_auth_change
