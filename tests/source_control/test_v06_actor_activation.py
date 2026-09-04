from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.app.modules.source_control.application.formal import (
    _formal_merge_actor_block_reason,
)
from control_plane.app.modules.source_control.ports import (
    ActorEligibilityContext,
    BindingEligibility,
    RequirementBindingContext,
)
from tests.source_control.test_v06_formal_application import _admission


@pytest.mark.parametrize(
    "actor_id,allowed",
    [("employee-1", True), ("merger-2", True), ("employee-1", False), ("merger-2", False)],
)
def test_formal_merge_checks_actual_actor_independently_of_owner(
    actor_id: str, allowed: bool
) -> None:
    admission = _admission()
    binding = RequirementBindingContext(
        requirement_id=admission.requirement_id,
        requirement_type="feat",
        requirement_title="Delivery",
        workspace_id=admission.workspace_id,
        work_item_id=admission.work_item_id,
        work_item_revision=admission.work_item_revision,
        repository_id=admission.repository_id,
        assignment_state="ASSIGNED",
        human_owner_id=admission.human_owner_id,
        required_capabilities=("work_item.execute",),
    )
    seen: list[ActorEligibilityContext] = []

    def evaluate(context: ActorEligibilityContext) -> BindingEligibility:
        seen.append(context)
        return BindingEligibility(eligible=allowed)

    dependencies: Any = SimpleNamespace(
        requirement=SimpleNamespace(binding_context=lambda _: binding),
        eligibility=SimpleNamespace(evaluate=evaluate),
    )
    result = _formal_merge_actor_block_reason(
        admission, actor_id=actor_id, dependencies=dependencies
    )
    assert result == (None if allowed else "MERGE_ACTOR_INELIGIBLE")
    assert len(seen) == 1
    assert isinstance(seen[0], ActorEligibilityContext)
    assert seen[0].actor_id == actor_id
    assert seen[0].required_capabilities == ("merge_request.merge",)
