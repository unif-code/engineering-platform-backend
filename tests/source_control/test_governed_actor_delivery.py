from dataclasses import replace
from typing import Any

import pytest

from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlFormalRepository,
)
from control_plane.app.modules.source_control.application.formal import (
    accept_formal_delivery_request,
    process_formal_delivery_request,
    reconcile_formal_delivery_effect,
)
from control_plane.app.modules.source_control.domain import FormalDeliveryRequestKind
from control_plane.app.modules.source_control.ports import (
    ActorEligibilityContext,
    BindingEligibility,
    GitLabMergeRequestSnapshot,
    GitLabResultUnknown,
)
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_v06_formal_application import (
    FakeEligibility,
    FakeFormalGitLab,
    FakeRequirementFormalDelivery,
    ReconciliationClock,
    _admission,
    _dependencies,
    _envelope,
    _seed,
)


@pytest.mark.parametrize("phase", ["admission", "dispatch", "reconciliation"])
def test_revoked_create_actor_never_becomes_formal_delivery(
    isolated_source_control_database: IsolatedSourceControlDatabase, phase: str
) -> None:
    database = isolated_source_control_database
    _seed(database)
    owner = FakeRequirementFormalDelivery(_admission())
    eligibility = FakeEligibility(eligible=phase != "admission")

    class Provider(FakeFormalGitLab):
        def list_merge_requests(
            self, *args: Any, **kwargs: Any
        ) -> list[GitLabMergeRequestSnapshot]:
            if phase == "dispatch":
                eligibility.eligible = False
            return super().list_merge_requests(*args, **kwargs)

    provider = Provider()
    if phase == "reconciliation":
        provider.create_error = GitLabResultUnknown("unknown")
    dependencies = _dependencies(database, owner, provider, eligibility=eligibility)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000691")
    with database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db), envelope, dependencies=dependencies
        )
    result = process_formal_delivery_request(
        message_id=envelope.message_id, dependencies=dependencies
    )
    if phase == "reconciliation":
        assert result.effect is not None
        eligibility.eligible = False
        provider.candidates = [provider.current]
        result = reconcile_formal_delivery_effect(
            effect_id=result.effect.id,
            dependencies=replace(dependencies, clock=ReconciliationClock()),
        )
    assert result.blocked_reason == "OWNER_INELIGIBLE"
    assert result.binding is None
    assert not owner.ready
    if phase != "reconciliation":
        assert provider.created == 0


@pytest.mark.parametrize("revoked", [None, "owner", "merger"])
def test_independent_merger_keeps_own_capability_and_live_owner_requirement(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    revoked: str | None,
) -> None:
    database = isolated_source_control_database
    _seed(database)
    owner = FakeRequirementFormalDelivery(_admission())
    grants = {
        "employee-1": {"code.change", "formal_merge_request.request"},
        "independent-merger": {"merge_request.merge"},
    }

    class Eligibility(FakeEligibility):
        def evaluate(self, context: ActorEligibilityContext) -> BindingEligibility:
            return BindingEligibility(
                eligible=set(context.required_capabilities) <= grants.get(context.actor_id, set())
            )

    provider = FakeFormalGitLab()
    dependencies = _dependencies(database, owner, provider, eligibility=Eligibility())
    create = _envelope(message_id="91000000-0000-0000-0000-000000000693")
    with database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db), create, dependencies=dependencies
        )
    created = process_formal_delivery_request(
        message_id=create.message_id, dependencies=dependencies
    )
    assert created.binding is not None
    owner.admission = _admission(
        requirement_revision=14,
        work_item_revision=12,
        formal_merge_request_binding_id=created.binding.id,
        formal_review_decision_id="99000000-0000-0000-0000-000000000694",
    )
    merge = _envelope(
        message_id="91000000-0000-0000-0000-000000000694",
        kind=FormalDeliveryRequestKind.MERGE_MR,
        binding_id=created.binding.id,
        review_id=owner.admission.formal_review_decision_id,
        requirement_revision=14,
        work_item_revision=12,
    ).model_copy(update={"actor_id": "independent-merger"})
    with database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db), merge, dependencies=dependencies
        )
    if revoked:
        grants["employee-1" if revoked == "owner" else "independent-merger"].clear()
    result = process_formal_delivery_request(message_id=merge.message_id, dependencies=dependencies)
    if revoked:
        assert result.blocked_reason == "MERGE_ACTOR_INELIGIBLE"
        assert provider.merged == 0 and not owner.merged
    else:
        assert result.blocked_reason is None
        assert provider.merged == 1 and len(owner.merged) == 1
