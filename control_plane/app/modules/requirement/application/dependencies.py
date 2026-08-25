from dataclasses import dataclass

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.requirement.ports import (
    AssignmentGuardPort,
    ClockPort,
    RandomPort,
    RequirementRepositoryFactory,
    RouteSnapshotPort,
)
from control_plane.app.shared.security import SecretManagerPort


@dataclass(frozen=True, slots=True)
class RequirementDependencies:
    repository_factory: RequirementRepositoryFactory
    audit: TransactionalAuditAppender
    clock: ClockPort
    random: RandomPort
    route_snapshots: RouteSnapshotPort
    assignment_guard: AssignmentGuardPort
    secret_manager: SecretManagerPort
