from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

import control_plane.app.modules.requirement as requirement
from control_plane.app.modules.requirement.adapters.delivery_gates import DeliveryGatePolicyAdapter
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.delivery_policy_helpers import resolved_policy
from tests.requirement.test_commands import Actor
from tests.requirement.test_v06_acceptance_commands import (
    StaticDeliveryReviewerGuard,
    _selection_fixture,
)
from tests.requirement.test_v06_formal_delivery_commands import _approved_requirement


def test_default_reassigns_with_frozen_capabilities_and_old_assignee_cannot_decide(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    command = getattr(requirement, "reassign_delivery_gate", None)
    assert command is not None, "Delivery Gate needs its own ordinary reassignment command"
    database = isolated_requirement_database
    requested, evidence, dependencies = _selection_fixture(database, key_suffix="reassign-governed")
    policy = SimpleNamespace(
        resolved_snapshot=lambda: resolved_policy(2, acceptance=("code.change",))
    )
    seen = []
    granted = {"candidate": {"requirement.acceptance.decide"}}

    def evaluate(
        *, actor_id: str, workspace_id: str, required_capabilities: tuple[str, ...]
    ) -> requirement.DeliveryReviewerEligibilitySnapshot:
        seen.append((actor_id, required_capabilities))
        return StaticDeliveryReviewerGuard(
            set(required_capabilities) <= granted.get(actor_id, set())
        ).evaluate(
            actor_id=actor_id,
            workspace_id=workspace_id,
            required_capabilities=required_capabilities,
        )

    dependencies = replace(
        dependencies,
        delivery_gate_policies=DeliveryGatePolicyAdapter(cast(Any, policy)),
        delivery_reviewer_guard=SimpleNamespace(evaluate=evaluate),
    )
    with database.runtime.begin() as db:
        selected = requirement.select_integration_baseline(
            db,
            requirement_id=requested.requirement.id,
            delivery_snapshot_id=requested.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=requested.requirement.revision,
            expected_requirement_version=requested.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="reassign-select",
            dependencies=dependencies,
        )
        confirmation = requirement.confirm_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            selection_id=selected.selection.id,
            expected_revision=selected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="reassign-confirm",
            dependencies=dependencies,
        )
    policy.resolved_snapshot = lambda: resolved_policy(3)
    args = dict(
        requirement_id=selected.requirement.id,
        gate_id=confirmation.gate.id,
        candidate_id="candidate",
        expected_gate_revision=confirmation.gate.revision,
        reason="Delegate acceptance",
        actor=Actor("employee-1"),
        idempotency_key="reassign",
        dependencies=dependencies,
    )
    granted["candidate"].add("code.change")
    with pytest.raises(requirement.GateReviewerIneligible, match="employee-1"):
        with database.runtime.begin() as db:
            command(db, **args)
    granted["employee-1"] = {"requirement.delivery_gate.assign"}
    granted["candidate"].remove("code.change")
    with pytest.raises(requirement.GateReviewerIneligible):
        with database.runtime.begin() as db:
            command(db, **args)
    granted["candidate"].add("code.change")
    with database.runtime.begin() as db:
        result = command(db, **args)
    with database.runtime.begin() as db:
        replay = command(db, **args)
    assert result == replay
    assert result.assignment.default_reviewer_id == "employee-1"
    assert result.assignment.current_reviewer_id == "candidate"
    assert result.assignment.revision == 2
    assert result.gate.revision == confirmation.gate.revision + 1
    assert ("employee-1", ("requirement.delivery_gate.assign",)) in seen
    assert ("candidate", ("requirement.acceptance.decide", "code.change")) in seen
    with pytest.raises(requirement.GateReviewerMismatch):
        with database.runtime.begin() as db:
            command(
                db,
                **{
                    **args,
                    "actor": Actor("candidate"),
                    "idempotency_key": "forward",
                    "expected_gate_revision": result.gate.revision,
                },
            )
    with pytest.raises(requirement.GateReviewerMismatch):
        with database.runtime.begin() as db:
            requirement.decide_requirement_acceptance(
                db,
                requirement_id=selected.requirement.id,
                gate_id=confirmation.gate.id,
                outcome=requirement.DecisionOutcome.APPROVED,
                reason="Stale assignment",
                expected_revision=confirmation.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key="old-decide",
                dependencies=dependencies,
            )
    decision_args: dict[str, Any] = dict(
        requirement_id=selected.requirement.id,
        gate_id=confirmation.gate.id,
        outcome=requirement.DecisionOutcome.APPROVED,
        reason="Qualified frozen decision",
        expected_revision=confirmation.requirement.revision,
        actor=Actor("candidate"),
        idempotency_key="candidate-decide",
        dependencies=dependencies,
    )
    granted["candidate"].remove("code.change")
    with pytest.raises(requirement.GateReviewerIneligible):
        with database.runtime.begin() as db:
            requirement.decide_requirement_acceptance(db, **decision_args)
    granted["candidate"].add("code.change")
    with database.runtime.begin() as db:
        decided = requirement.decide_requirement_acceptance(db, **decision_args)
    assert decided.decision.reviewer_id == "candidate"
    assert decided.decision.eligibility_snapshot["required_capabilities"] == [
        "requirement.acceptance.decide",
        "code.change",
    ]
    with pytest.raises(requirement.GateAlreadyDecided):
        with database.runtime.begin() as db:
            command(
                db,
                **{
                    **args,
                    "expected_gate_revision": decided.gate.revision,
                    "idempotency_key": "after-decision",
                },
            )


def test_formal_reassignment_keeps_exact_head_and_frozen_review_capabilities(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    database = isolated_requirement_database
    approved, evidence, dependencies = _approved_requirement(database)
    item = evidence.work_items[0]
    with database.runtime.begin() as db:
        requested = requirement.request_formal_merge_request(
            db,
            requirement_id=approved.requirement.id,
            work_item_id=item.work_item_id,
            expected_revision=approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="formal-delegate-request",
            dependencies=dependencies,
        )
    resolved = resolved_policy(4, formal=("code.change",))
    assignment = requirement.DeliveryGatePolicySnapshot(
        version=4,
        default_reviewer_id="employee-1",
        policy_code="FORMAL_REVIEW_ORGANIZATION",
        snapshot_hash="sha256:" + resolved.snapshot_hash,
        resolution_snapshot={"policy": asdict(resolved), "rule": "SELF"},
    )
    with database.runtime.begin() as db:
        ready = requirement.record_formal_mr_ready(
            db,
            work_item_id=item.work_item_id,
            binding_id="96000000-0000-0000-0000-000000000692",
            head_sha=item.task_commit_sha,
            expected_revision=requested.work_item.revision,
            assignment=assignment,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="formal-delegate-ready",
            correlation_id="formal-delegate",
            dependencies=dependencies,
        )
    with database.runtime.begin() as db:
        reassigned = requirement.reassign_delivery_gate(
            db,
            requirement_id=approved.requirement.id,
            gate_id=ready.gate.id,
            candidate_id="reviewer",
            expected_gate_revision=ready.gate.revision,
            actor=Actor("employee-1"),
            reason="Delegate formal review",
            idempotency_key="formal-delegate",
            dependencies=dependencies,
        )
    assert reassigned.gate.subject_head_sha == item.task_commit_sha
    assert (
        reassigned.assignment.resolution_snapshot["policy"]
        == ready.assignment.resolution_snapshot["policy"]
    )
    seen: list[tuple[str, ...]] = []

    def evaluate(
        *, actor_id: str, workspace_id: str, required_capabilities: tuple[str, ...]
    ) -> requirement.DeliveryReviewerEligibilitySnapshot:
        seen.append(required_capabilities)
        return StaticDeliveryReviewerGuard("code.change" not in required_capabilities).evaluate(
            actor_id=actor_id,
            workspace_id=workspace_id,
            required_capabilities=required_capabilities,
        )

    dependencies = replace(dependencies, delivery_reviewer_guard=SimpleNamespace(evaluate=evaluate))
    with pytest.raises(requirement.GateReviewerIneligible):
        with database.runtime.begin() as db:
            requirement.decide_formal_review(
                db,
                requirement_id=approved.requirement.id,
                gate_id=ready.gate.id,
                outcome=requirement.DecisionOutcome.APPROVED,
                expected_revision=ready.requirement.revision,
                reason="Review exact head",
                actor=Actor("reviewer"),
                idempotency_key="formal-delegate-decide",
                dependencies=dependencies,
            )
    assert seen == [("merge_request.review", "code.change")]
