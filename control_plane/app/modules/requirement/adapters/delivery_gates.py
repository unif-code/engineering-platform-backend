from dataclasses import asdict, dataclass

from control_plane.app.modules.authorization import ActorQualificationPort
from control_plane.app.modules.requirement.adapters.gate_policy_runtime import (
    RequirementPolicyRuntime,
)
from control_plane.app.modules.requirement.ports.runtime import (
    DeliveryGatePolicySnapshot,
    DeliveryReviewerEligibilitySnapshot,
)


@dataclass(frozen=True, slots=True)
class DeliveryGatePolicyAdapter:
    policy: RequirementPolicyRuntime

    def requirement_acceptance(
        self,
        *,
        workspace_id: str,
        requirement_created_by: str,
    ) -> DeliveryGatePolicySnapshot:
        if not requirement_created_by.strip():
            raise ValueError("Requirement creator is unavailable")
        resolved = self.policy.resolved_snapshot()
        return DeliveryGatePolicySnapshot(
            version=resolved.version,
            default_reviewer_id=requirement_created_by,
            policy_code="REQUIREMENT_ACCEPTANCE_CREATOR",
            snapshot_hash=f"sha256:{resolved.snapshot_hash}",
            resolution_snapshot={
                "workspaceId": workspace_id,
                "rule": resolved.policy.acceptance_default_route,
                "requirementCreatedBy": requirement_created_by,
                "policy": asdict(resolved),
            },
        )


@dataclass(frozen=True, slots=True)
class DeliveryReviewerGuardAdapter:
    qualification: ActorQualificationPort

    def evaluate(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        required_capabilities: tuple[str, ...],
    ) -> DeliveryReviewerEligibilitySnapshot:
        facts = self.qualification.evaluate(actor_id, workspace_id, required_capabilities)
        return DeliveryReviewerEligibilitySnapshot(
            eligible=facts.eligible,
            actor_id=facts.actor_id,
            required_capabilities=facts.required_capabilities,
            workspace_id=facts.workspace_id,
            account_version=facts.account.version if facts.account else None,
            workspace_version=facts.workspace.version if facts.workspace else None,
            principal_version=facts.principal.version if facts.principal else None,
            snapshot_hash=facts.snapshot_hash,
            details=facts.model_dump(mode="json"),
        )
