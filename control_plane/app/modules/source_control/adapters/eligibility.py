from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

import control_plane.app.modules.authorization as authorization
import control_plane.app.modules.identity as identity
import control_plane.app.modules.workspace as workspace
from control_plane.app.modules.source_control.ports import (
    BindingEligibility,
    RequirementBindingContext,
)


@dataclass(frozen=True, slots=True)
class CurrentOwnerEligibilityAdapter:
    identity_engine: Engine
    identity_dependencies: Any
    workspace_engine: Engine
    workspace_dependencies: Any
    authorization_engine: Engine
    authorization_dependencies: Any

    def evaluate(self, context: RequirementBindingContext) -> BindingEligibility:
        owner_id = context.human_owner_id
        if context.assignment_state != "ASSIGNED" or owner_id is None:
            return BindingEligibility(eligible=False, reason_code="OWNER_UNASSIGNED")
        try:
            with self.identity_engine.begin() as db:
                account = identity.get_account(
                    db,
                    account_id=owner_id,
                    dependencies=self.identity_dependencies,
                )
            if account.status.value != "ENABLED":
                return BindingEligibility(eligible=False, reason_code="OWNER_INELIGIBLE")
            with self.workspace_engine.begin() as db:
                formal_member = workspace.is_formal_member(
                    db,
                    workspace_id=context.workspace_id,
                    account_id=owner_id,
                    dependencies=self.workspace_dependencies,
                )
            if not formal_member:
                return BindingEligibility(eligible=False, reason_code="OWNER_INELIGIBLE")
            for capability in context.required_capabilities:
                with self.authorization_engine.begin() as db:
                    grants = authorization.effective_grants(
                        db,
                        principal_id=owner_id,
                        capability=capability,
                        scope=authorization.Scope.workspace(context.workspace_id),
                        dependencies=self.authorization_dependencies,
                    )
                if not grants:
                    return BindingEligibility(eligible=False, reason_code="OWNER_INELIGIBLE")
        except Exception:
            return BindingEligibility(eligible=False, reason_code="OWNER_INELIGIBLE")
        return BindingEligibility(eligible=True)
