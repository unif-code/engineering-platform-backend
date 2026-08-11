from dataclasses import dataclass

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.organization.ports import (
    ClockPort,
    IdentityAccountLookupPort,
    MembershipChangePort,
    OrganizationRepositoryFactory,
    RandomPort,
)
from control_plane.app.shared.security import SecretManagerPort


@dataclass(frozen=True, slots=True)
class OrganizationDependencies:
    repository_factory: OrganizationRepositoryFactory
    identity: IdentityAccountLookupPort
    audit: TransactionalAuditAppender
    on_membership_change: MembershipChangePort
    clock: ClockPort
    random: RandomPort
    secret_manager: SecretManagerPort
