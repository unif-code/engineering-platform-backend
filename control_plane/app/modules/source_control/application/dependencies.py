from dataclasses import dataclass

from sqlalchemy import Engine

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.source_control.ports import (
    ActorEligibilityPort,
    AgentDeliveryPolicyPort,
    AgentDeliveryRepositoryFactory,
    AgentExecutionBindingPort,
    AgentPushBrokerPort,
    AgentPushGrantIssuerPort,
    ClockPort,
    FormalReviewRoutingPort,
    GitLabFormalMergeRequestPort,
    GitLabMergeRequestPort,
    GitLabPort,
    RandomPort,
    RequirementBindingPort,
    RequirementDeliveryPort,
    RequirementEvidencePort,
    RequirementFormalDeliveryPort,
    SecretReferencePort,
    SourceControlEvidenceRepositoryFactory,
    SourceControlFormalRepositoryFactory,
    SourceControlIntegrationRepositoryFactory,
    SourceControlPolicyPort,
    SourceControlRepositoryFactory,
)


@dataclass(frozen=True, slots=True)
class SourceControlDependencies:
    repository_factory: SourceControlRepositoryFactory
    engine: Engine
    requirement: RequirementBindingPort | None
    eligibility: ActorEligibilityPort | None
    audit: TransactionalAuditAppender
    clock: ClockPort
    random: RandomPort
    gitlab: GitLabPort | None = None
    policy: SourceControlPolicyPort | None = None
    webhook_secrets: SecretReferencePort | None = None
    delivery_repository_factory: SourceControlIntegrationRepositoryFactory | None = None
    requirement_delivery: RequirementDeliveryPort | None = None
    gitlab_merge_requests: GitLabMergeRequestPort | None = None
    agent_delivery_repository_factory: AgentDeliveryRepositoryFactory | None = None
    agent_execution_bindings: AgentExecutionBindingPort | None = None
    agent_push_broker: AgentPushBrokerPort | None = None
    agent_push_grants: AgentPushGrantIssuerPort | None = None
    agent_delivery_policy: AgentDeliveryPolicyPort | None = None
    gitlab_formal_merge_requests: GitLabFormalMergeRequestPort | None = None
    evidence_repository_factory: SourceControlEvidenceRepositoryFactory | None = None
    requirement_evidence: RequirementEvidencePort | None = None
    formal_repository_factory: SourceControlFormalRepositoryFactory | None = None
    requirement_formal_delivery: RequirementFormalDeliveryPort | None = None
    formal_review_routing: FormalReviewRoutingPort | None = None
