from dataclasses import dataclass

from control_plane.app.modules.authorization import ActorQualificationPort
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import (
    ActorEligibilityContext,
    BindingEligibility,
)


@dataclass(frozen=True, slots=True)
class CurrentActorEligibilityAdapter:
    qualification: ActorQualificationPort

    def evaluate(self, context: ActorEligibilityContext) -> BindingEligibility:
        try:
            facts = self.qualification.evaluate(
                context.actor_id, context.workspace_id, context.required_capabilities
            )
            eligible = (
                facts.eligible
                and facts.actor_id == context.actor_id
                and facts.workspace_id == context.workspace_id
                and facts.required_capabilities == context.required_capabilities
            )
            return BindingEligibility(
                eligible=eligible,
                reason_code=None if eligible else SourceControlReason.OWNER_INELIGIBLE,
                qualification_snapshot=facts.model_dump(mode="json"),
            )
        except Exception:
            return BindingEligibility(
                eligible=False, reason_code=SourceControlReason.OWNER_INELIGIBLE
            )
