from dataclasses import asdict, dataclass

from sqlalchemy import Engine

import control_plane.app.modules.organization as organization
from control_plane.app.modules.authorization import ActorQualificationPort
from control_plane.app.modules.requirement import RequirementPolicyRuntime
from control_plane.app.modules.source_control.domain import FormalReviewRoutingSnapshot


@dataclass(frozen=True, slots=True)
class GovernedFormalReviewRoutingAdapter:
    organization_engine: Engine
    organization_dependencies: organization.OrganizationDependencies
    policy: RequirementPolicyRuntime
    qualification: ActorQualificationPort

    def resolve(
        self, *, workspace_id: str, repository_id: str, work_item_id: str, human_owner_id: str
    ) -> FormalReviewRoutingSnapshot:
        resolved = self.policy.resolved_snapshot()
        with self.organization_engine.connect() as db:
            context = organization.reporting_context(
                db, account_id=human_owner_id, dependencies=self.organization_dependencies
            )
        route = dict(resolved.policy.formal_review_default_routes).get(context.kind)
        if route not in ("SELF", "DIRECT_LEADER") or context.account_id != human_owner_id:
            raise ValueError("Unsupported Formal Review routing")
        capabilities = resolved.policy.formal_review_required_capabilities
        facts = self.qualification.evaluate(context.reviewer_id, workspace_id, capabilities)
        with self.organization_engine.connect() as db:
            after = organization.reporting_context(
                db, account_id=human_owner_id, dependencies=self.organization_dependencies
            )
        if context != after:
            raise ValueError("Relevant Organization facts changed")
        if (
            not facts.eligible
            or facts.actor_id != context.reviewer_id
            or facts.workspace_id != workspace_id
            or facts.required_capabilities != capabilities
        ):
            raise ValueError("Formal Reviewer is not qualified")
        return FormalReviewRoutingSnapshot(
            default_reviewer_id=context.reviewer_id,
            policy_code="FORMAL_REVIEW_ORGANIZATION",
            policy_version=resolved.version,
            policy_snapshot_hash=f"sha256:{resolved.snapshot_hash}",
            resolution_snapshot={
                "workspaceId": workspace_id,
                "repositoryId": repository_id,
                "workItemId": work_item_id,
                "humanOwnerId": human_owner_id,
                "rule": route,
                "organization": context.model_dump(mode="json"),
                "policy": asdict(resolved),
                "qualification": facts.model_dump(mode="json"),
            },
        )
