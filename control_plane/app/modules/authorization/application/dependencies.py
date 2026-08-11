from collections.abc import Callable
from dataclasses import dataclass

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.authorization.ports import (
    AuthorizationRepositoryFactory,
    ClockPort,
    IdentitySessionPort,
    RandomPort,
    WorkspaceMembershipPort,
)
from control_plane.app.shared.security import SecretManagerPort


@dataclass(frozen=True, slots=True)
class AuthorizationDependencies:
    repository_factory: AuthorizationRepositoryFactory
    audit: TransactionalAuditAppender
    clock: ClockPort
    random: RandomPort
    secret_manager: SecretManagerPort


@dataclass(frozen=True, slots=True)
class DecisionDependencies:
    identity: IdentitySessionPort
    workspace: WorkspaceMembershipPort
    reconcile: Callable[[str], bool] | None = None
