from dataclasses import dataclass

from sqlalchemy import Engine

from control_plane.app.modules.audit import TransactionalAuditAppender
from control_plane.app.modules.source_control.ports import (
    ClockPort,
    GitLabPort,
    OwnerEligibilityPort,
    RandomPort,
    RequirementBindingPort,
    SecretReferencePort,
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
