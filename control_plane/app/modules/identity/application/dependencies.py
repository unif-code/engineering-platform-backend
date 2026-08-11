from dataclasses import dataclass

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.identity.ports.policy import EffectivePolicyPort
from control_plane.app.modules.identity.ports.repository import IdentityRepositoryFactory
from control_plane.app.modules.identity.ports.runtime import (
    AuthorizationChangePort,
    ClockPort,
    RandomPort,
)
from control_plane.app.shared.security import SecretManagerPort


@dataclass(frozen=True, slots=True)
class IdentityDependencies:
    repository_factory: IdentityRepositoryFactory
    secret_manager: SecretManagerPort
    policy: EffectivePolicyPort
    clock: ClockPort
    random: RandomPort
    audit: TransactionalAuditAppender
    on_auth_change: AuthorizationChangePort
