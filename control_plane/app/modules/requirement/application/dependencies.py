from dataclasses import dataclass

from control_plane.app.modules.audit import AuditEventRepository, TransactionalAuditAppender
from control_plane.app.modules.requirement.ports import (
    ArtifactPort,
    AssignmentGuardPort,
    ClockPort,
    DeliveryGatePolicyPort,
    DeliveryReviewerGuardPort,
    GatePolicyPort,
    GateReviewerGuardPort,
    IntegrationBaselineEvidencePort,
    RandomPort,
    RequirementRepositoryFactory,
    RouteSnapshotPort,
)
from control_plane.app.shared.security import SecretManagerPort


@dataclass(frozen=True, slots=True)
class RequirementDependencies:
    repository_factory: RequirementRepositoryFactory
    audit: TransactionalAuditAppender
    denial_audit: AuditEventRepository
    clock: ClockPort
    random: RandomPort
    route_snapshots: RouteSnapshotPort
    assignment_guard: AssignmentGuardPort
    secret_manager: SecretManagerPort
    artifacts: ArtifactPort | None = None
    gate_policies: GatePolicyPort | None = None
    reviewer_guard: GateReviewerGuardPort | None = None
    integration_evidence: IntegrationBaselineEvidencePort | None = None
    delivery_gate_policies: DeliveryGatePolicyPort | None = None
    delivery_reviewer_guard: DeliveryReviewerGuardPort | None = None
