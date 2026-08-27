from dataclasses import dataclass

from sqlalchemy import Engine

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.source_control.ports import (
    ClockPort,
    GitLabMergeRequestPort,
    GitLabPort,
    OwnerEligibilityPort,
    RandomPort,
    RequirementBindingPort,
    RequirementDeliveryPort,
    SecretReferencePort,
    SourceControlIntegrationRepositoryFactory,
    SourceControlPolicyPort,
    SourceControlRepositoryFactory,
)


@dataclass(frozen=True, slots=True)
class SourceControlDependencies:
    repository_factory: SourceControlRepositoryFactory
    engine: Engine
    requirement: RequirementBindingPort | None
    eligibility: OwnerEligibilityPort | None
    audit: TransactionalAuditAppender
    clock: ClockPort
    random: RandomPort
    gitlab: GitLabPort | None = None
    policy: SourceControlPolicyPort | None = None
    webhook_secrets: SecretReferencePort | None = None
    delivery_repository_factory: SourceControlIntegrationRepositoryFactory | None = None
    requirement_delivery: RequirementDeliveryPort | None = None
    gitlab_merge_requests: GitLabMergeRequestPort | None = None
