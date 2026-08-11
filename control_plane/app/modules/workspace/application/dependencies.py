from dataclasses import dataclass

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.workspace.ports import (
    ClockPort,
    IdentityAccountLookupPort,
    OrganizationReportsPort,
    RandomPort,
    WorkspaceRepositoryFactory,
)
from control_plane.app.shared.security import SecretManagerPort


@dataclass(frozen=True, slots=True)
class WorkspaceDependencies:
    repository_factory: WorkspaceRepositoryFactory
    identity: IdentityAccountLookupPort
    organization: OrganizationReportsPort
    audit: TransactionalAuditAppender
    clock: ClockPort
    random: RandomPort
    secret_manager: SecretManagerPort
