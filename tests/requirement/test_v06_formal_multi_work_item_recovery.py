from dataclasses import replace

import pytest
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    AcceptanceDecisionResult,
    ArtifactEvidenceReference,
    DecisionOutcome,
    DeliveryGatePolicySnapshot,
    IntegrationBaselineEvidenceSnapshot,
    IntegrationBaselineEvidenceWorkItem,
    RequirementDeliverySnapshot,
    RequirementDependencies,
    RequirementState,
    WorkItemState,
    add_work_item,
    confirm_requirement_acceptance,
    decide_requirement_acceptance,
    get_requirement,
    record_integration_merged,
    record_integration_mr_ready,
    request_integration_baseline,
    request_integration_merge,
    request_integration_merge_request,
    select_integration_baseline,
    start_requirement_preparation,
)
from control_plane.app.modules.requirement.adapters import SqlAlchemyRequirementRepository
from control_plane.app.modules.requirement.application.formal import (
    decide_formal_review,
    record_formal_delivery_blocked,
    record_formal_merged,
    record_formal_mr_ready,
    request_formal_merge,
    request_formal_merge_request,
)
from control_plane.app.modules.requirement.domain import (
    DecisionValidity,
    FormalDeliveryBlocked,
    FormalDeliveryBlockedReason,
    FormalDeliveryCommandResult,
    FormalDeliveryConflict,
    FormalDeliveryState,
    IntegrationDeliveryState,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.delivery_policy_helpers import frozen_policy, resolved_policy
from tests.requirement.test_baseline_gate import ARTIFACT_HASH_V1, _gate_dependencies
from tests.requirement.test_commands import NOW, Actor, _create
from tests.requirement.test_v06_acceptance_commands import (
    StaticDeliveryPolicies,
    StaticDeliveryReviewerGuard,
    StaticEvidence,
)

_A_INITIAL_INTEGRATION_BINDING = "97000000-0000-0000-0000-000000000701"
_B_INITIAL_INTEGRATION_BINDING = "97000000-0000-0000-0000-000000000702"
_A_REWORK_INTEGRATION_BINDING = "97000000-0000-0000-0000-000000000703"
_B_REWORK_INTEGRATION_BINDING = "97000000-0000-0000-0000-000000000704"
_A_FORMAL_BINDING = "98000000-0000-0000-0000-000000000701"
_B_FORMAL_BINDING = "98000000-0000-0000-0000-000000000702"
_A_INITIAL_HEAD = "b" * 40
_A_REWORK_HEAD = "e" * 40
_B_HEAD = "d" * 40


def _formal_routing() -> DeliveryGatePolicySnapshot:
    return DeliveryGatePolicySnapshot(
        version=4,
        default_reviewer_id="employee-1",
        policy_code="FORMAL_REVIEW_WORK_ITEM_OWNER",
        snapshot_hash="sha256:" + resolved_policy(4).snapshot_hash,
        resolution_snapshot=frozen_policy(),
    )


def _evidence(
    *,
    evidence_id: str,
    evidence_hash_character: str,
    snapshot: RequirementDeliverySnapshot,
    requirement_id: str,
    a_work_item_id: str,
    a_head: str,
    b_work_item_id: str,
) -> IntegrationBaselineEvidenceSnapshot:
    items = {
        a_work_item_id: IntegrationBaselineEvidenceWorkItem(
            work_item_id=a_work_item_id,
            repository_id="repository-1",
            task_commit_sha=a_head,
            integration_merge_commit_sha="c" * 40,
            artifact_references=(
                ArtifactEvidenceReference(
                    artifact_id="sdd-1",
                    artifact_version="version-1",
                    artifact_hash=ARTIFACT_HASH_V1,
                ),
            ),
        ),
        b_work_item_id: IntegrationBaselineEvidenceWorkItem(
            work_item_id=b_work_item_id,
            repository_id="repository-2",
            task_commit_sha=_B_HEAD,
            integration_merge_commit_sha="f" * 40,
            artifact_references=(
                ArtifactEvidenceReference(
                    artifact_id="sdd-1",
                    artifact_version="version-1",
                    artifact_hash=ARTIFACT_HASH_V1,
                ),
            ),
        ),
    }
    return IntegrationBaselineEvidenceSnapshot(
        id=evidence_id,
        evidence_hash="sha256:" + evidence_hash_character * 64,
        delivery_snapshot_id=snapshot.id,
        delivery_snapshot_hash=snapshot.snapshot_hash,
        requirement_id=requirement_id,
        requirement_version=snapshot.requirement_version,
        required_work_item_set_version=snapshot.required_work_item_set_version,
        required_work_item_set_hash=snapshot.required_work_item_set_hash,
        currentness_state="CURRENT",
        currentness_reasons=(),
        work_items=tuple(items[work_item_id] for work_item_id in snapshot.work_item_ids),
        generated_at=NOW,
    )


def _accepted_two_work_item_requirement(
    database: IsolatedRequirementDatabase,
) -> tuple[
    AcceptanceDecisionResult,
    IntegrationBaselineEvidenceSnapshot,
    RequirementDependencies,
    str,
    str,
]:
    base_dependencies = _gate_dependencies()
    created = _create(database, idempotency_key="v06-formal-multi-create")
    with database.runtime.begin() as db:
        prepared = start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=created.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-prepare",
            dependencies=base_dependencies,
        )
    with database.runtime.begin() as db:
        added = add_work_item(
            db,
            requirement_id=created.requirement.id,
            repository_id="repository-2",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-add-b",
            dependencies=base_dependencies,
        )
    a_work_item_id = created.work_item.id
    b_work_item_id = added.work_item.id

    # The fixture starts at the already-proven V0.5 integration boundary. Every
    # recovery transition below this seed uses the public Requirement commands.
    with database.owner.begin() as db:
        db.execute(
            text("UPDATE requirement.requirement SET state='VERIFYING' WHERE id=:requirement_id"),
            {"requirement_id": created.requirement.id},
        )
        db.execute(
            text(
                "UPDATE requirement.work_item SET state='VERIFYING', "
                "repository_state='BOUND', base_commit_sha=:base_commit_sha, "
                "task_branch=:task_branch, integration_delivery_state='INTEGRATED', "
                "integration_merge_request_binding_id=:binding_id, "
                "integration_updated_at=:now WHERE id=:work_item_id"
            ),
            {
                "base_commit_sha": "a" * 40,
                "binding_id": _A_INITIAL_INTEGRATION_BINDING,
                "now": NOW,
                "task_branch": f"task/{a_work_item_id}",
                "work_item_id": a_work_item_id,
            },
        )
        db.execute(
            text(
                "UPDATE requirement.work_item SET state='VERIFYING', "
                "repository_state='BOUND', base_commit_sha=:base_commit_sha, "
                "task_branch=:task_branch, integration_delivery_state='INTEGRATED', "
                "integration_merge_request_binding_id=:binding_id, "
                "integration_updated_at=:now WHERE id=:work_item_id"
            ),
            {
                "base_commit_sha": "a" * 40,
                "binding_id": _B_INITIAL_INTEGRATION_BINDING,
                "now": NOW,
                "task_branch": f"task/{b_work_item_id}",
                "work_item_id": b_work_item_id,
            },
        )
    with database.runtime.connect() as db:
        integrated = get_requirement(
            db,
            requirement_id=created.requirement.id,
            dependencies=base_dependencies,
        )
    with database.runtime.begin() as db:
        requested = request_integration_baseline(
            db,
            requirement_id=created.requirement.id,
            expected_revision=integrated.requirement.revision,
            expected_requirement_version=integrated.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-snapshot-v1",
            dependencies=base_dependencies,
        )
    evidence = _evidence(
        evidence_id="95000000-0000-0000-0000-000000000701",
        evidence_hash_character="6",
        snapshot=requested.snapshot,
        requirement_id=created.requirement.id,
        a_work_item_id=a_work_item_id,
        a_head=_A_INITIAL_HEAD,
        b_work_item_id=b_work_item_id,
    )
    dependencies = replace(
        base_dependencies,
        integration_evidence=StaticEvidence(evidence),
        delivery_gate_policies=StaticDeliveryPolicies(),
        delivery_reviewer_guard=StaticDeliveryReviewerGuard(),
    )
    with database.runtime.begin() as db:
        selected = select_integration_baseline(
            db,
            requirement_id=created.requirement.id,
            delivery_snapshot_id=requested.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=requested.requirement.revision,
            expected_requirement_version=requested.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-select-v1",
            dependencies=dependencies,
        )
    with database.runtime.begin() as db:
        opened = confirm_requirement_acceptance(
            db,
            requirement_id=created.requirement.id,
            selection_id=selected.selection.id,
            expected_revision=selected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-acceptance-v1",
            dependencies=dependencies,
        )
    with database.runtime.begin() as db:
        accepted = decide_requirement_acceptance(
            db,
            requirement_id=created.requirement.id,
            gate_id=opened.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="Both exact integration subjects are accepted.",
            expected_revision=opened.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-approve-v1",
            dependencies=dependencies,
        )
    return accepted, evidence, dependencies, a_work_item_id, b_work_item_id


def test_negative_formal_review_refreshes_same_head_sibling_in_new_selection(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    accepted, initial_evidence, dependencies, a_work_item_id, b_work_item_id = (
        _accepted_two_work_item_requirement(isolated_requirement_database)
    )
    routing = _formal_routing()

    with isolated_requirement_database.runtime.begin() as db:
        a_requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=a_work_item_id,
            expected_revision=accepted.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-a-create-v1",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        a_ready = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=a_work_item_id,
            binding_id=_A_FORMAL_BINDING,
            head_sha=_A_INITIAL_HEAD,
            expected_revision=a_requested.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-multi-a-ready-v1",
            correlation_id="v06-formal-multi-a-ready-v1",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=a_ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-b-create-v1",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_ready = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=b_work_item_id,
            binding_id=_B_FORMAL_BINDING,
            head_sha=_B_HEAD,
            expected_revision=b_requested.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-multi-b-ready-v1",
            correlation_id="v06-formal-multi-b-ready-v1",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_approved = decide_formal_review(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            gate_id=b_ready.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="B is approved at its exact head.",
            expected_revision=b_ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-b-review-v1",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        a_rejected = decide_formal_review(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            gate_id=a_ready.gate.id,
            outcome=DecisionOutcome.CHANGES_REQUESTED,
            reason="A must return with a new exact head.",
            expected_revision=b_approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-a-review-v1",
            dependencies=dependencies,
        )

    assert a_rejected.requirement.state is RequirementState.IN_PROGRESS
    assert a_rejected.decision.validity is DecisionValidity.INVALIDATED
    with isolated_requirement_database.owner.connect() as db:
        work_items = {
            str(row.id): row
            for row in db.execute(
                text(
                    "SELECT id, state, integration_delivery_state, formal_delivery_state, "
                    "formal_merge_request_binding_id::text AS formal_binding_id "
                    "FROM requirement.work_item WHERE requirement_id=:requirement_id"
                ),
                {"requirement_id": accepted.requirement.id},
            )
        }
    assert (
        work_items[a_work_item_id].state,
        work_items[a_work_item_id].integration_delivery_state,
        work_items[a_work_item_id].formal_delivery_state,
        work_items[a_work_item_id].formal_binding_id,
    ) == (
        WorkItemState.IN_PROGRESS.value,
        IntegrationDeliveryState.IMPLEMENTING.value,
        FormalDeliveryState.MR_OPEN.value,
        _A_FORMAL_BINDING,
    )
    assert (
        work_items[b_work_item_id].state,
        work_items[b_work_item_id].integration_delivery_state,
        work_items[b_work_item_id].formal_delivery_state,
        work_items[b_work_item_id].formal_binding_id,
    ) == (
        WorkItemState.VERIFYING.value,
        IntegrationDeliveryState.INTEGRATED.value,
        FormalDeliveryState.MR_OPEN.value,
        _B_FORMAL_BINDING,
    )

    with isolated_requirement_database.runtime.begin() as db:
        reintegration_requested = request_integration_merge_request(
            db,
            requirement_id=accepted.requirement.id,
            work_item_id=a_work_item_id,
            expected_revision=a_rejected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-a-integration-create-v2",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        reintegration_ready = record_integration_mr_ready(
            db,
            work_item_id=a_work_item_id,
            binding_id=_A_REWORK_INTEGRATION_BINDING,
            expected_revision=reintegration_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-multi-a-integration-ready-v2",
            correlation_id="v06-formal-multi-a-integration-ready-v2",
            dependencies=dependencies,
        )
    assert reintegration_ready.requirement.state is RequirementState.VERIFYING
    with isolated_requirement_database.runtime.begin() as db:
        reintegration_merge_requested = request_integration_merge(
            db,
            requirement_id=accepted.requirement.id,
            work_item_id=a_work_item_id,
            expected_revision=reintegration_ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-a-integration-merge-v2",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        reintegrated = record_integration_merged(
            db,
            work_item_id=a_work_item_id,
            binding_id=_A_REWORK_INTEGRATION_BINDING,
            expected_revision=reintegration_merge_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-multi-a-integration-merged-v2",
            correlation_id="v06-formal-multi-a-integration-merged-v2",
            dependencies=dependencies,
        )

    with isolated_requirement_database.runtime.begin() as db:
        second_snapshot = request_integration_baseline(
            db,
            requirement_id=accepted.requirement.id,
            expected_revision=reintegrated.requirement.revision,
            expected_requirement_version=reintegrated.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-snapshot-v2",
            dependencies=dependencies,
        )
    second_evidence = _evidence(
        evidence_id="95000000-0000-0000-0000-000000000702",
        evidence_hash_character="7",
        snapshot=second_snapshot.snapshot,
        requirement_id=accepted.requirement.id,
        a_work_item_id=a_work_item_id,
        a_head=_A_REWORK_HEAD,
        b_work_item_id=b_work_item_id,
    )
    refreshed_dependencies = replace(
        dependencies,
        integration_evidence=StaticEvidence(second_evidence),
    )
    with isolated_requirement_database.runtime.begin() as db:
        second_selection = select_integration_baseline(
            db,
            requirement_id=accepted.requirement.id,
            delivery_snapshot_id=second_snapshot.snapshot.id,
            integration_baseline_id=second_evidence.id,
            expected_revision=second_snapshot.requirement.revision,
            expected_requirement_version=second_snapshot.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-select-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        second_acceptance = confirm_requirement_acceptance(
            db,
            requirement_id=accepted.requirement.id,
            selection_id=second_selection.selection.id,
            expected_revision=second_selection.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-acceptance-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        second_approved = decide_requirement_acceptance(
            db,
            requirement_id=accepted.requirement.id,
            gate_id=second_acceptance.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="The refreshed multi-item evidence is accepted.",
            expected_revision=second_acceptance.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-approve-v2",
            dependencies=refreshed_dependencies,
        )

    # B deliberately keeps the same exact head. A new Selection, not a changed
    # head, is what makes the invalidated Formal Review eligible for refresh.
    with isolated_requirement_database.runtime.begin() as db:
        b_refreshed_request = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=second_approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-b-create-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_second_gate = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=b_work_item_id,
            binding_id=_B_FORMAL_BINDING,
            head_sha=_B_HEAD,
            expected_revision=b_refreshed_request.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-multi-b-ready-v2",
            correlation_id="v06-formal-multi-b-ready-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        a_refreshed_request = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=a_work_item_id,
            expected_revision=b_second_gate.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-multi-a-create-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        a_second_gate = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=a_work_item_id,
            binding_id=_A_FORMAL_BINDING,
            head_sha=_A_REWORK_HEAD,
            expected_revision=a_refreshed_request.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-multi-a-ready-v2",
            correlation_id="v06-formal-multi-a-ready-v2",
            dependencies=refreshed_dependencies,
        )

    assert b_second_gate.gate.id != b_ready.gate.id
    assert b_second_gate.gate.subject_head_sha == _B_HEAD
    assert a_second_gate.gate.id != a_ready.gate.id
    assert a_second_gate.gate.subject_head_sha == _A_REWORK_HEAD
    with isolated_requirement_database.owner.connect() as db:
        selections = db.execute(
            text(
                "SELECT id::text, invalidated_at IS NULL AS is_current "
                "FROM requirement.integration_baseline_selection "
                "WHERE requirement_id=:requirement_id ORDER BY selected_at, id"
            ),
            {"requirement_id": accepted.requirement.id},
        ).all()
        gates = db.execute(
            text(
                "SELECT id::text, selection_id::text, state, subject_head_sha "
                "FROM requirement.delivery_gate "
                "WHERE requirement_id=:requirement_id "
                "AND gate_type='FORMAL_MR_REVIEW' ORDER BY created_at, id"
            ),
            {"requirement_id": accepted.requirement.id},
        ).all()
        decisions = db.execute(
            text(
                "SELECT gate_id::text, outcome, validity "
                "FROM requirement.delivery_decision "
                "WHERE gate_id IN (:a_gate_id, :b_gate_id) ORDER BY gate_id"
            ),
            {"a_gate_id": a_ready.gate.id, "b_gate_id": b_ready.gate.id},
        ).all()

    assert {row[0]: row[1] for row in selections} == {
        accepted.selection.id: False,
        second_selection.selection.id: True,
    }
    gates_by_id = {row[0]: row for row in gates}
    assert gates_by_id[a_ready.gate.id][2] == "INVALIDATED"
    assert gates_by_id[b_ready.gate.id][2] == "INVALIDATED"
    assert gates_by_id[a_second_gate.gate.id][1:] == (
        second_selection.selection.id,
        "OPEN",
        _A_REWORK_HEAD,
    )
    assert gates_by_id[b_second_gate.gate.id][1:] == (
        second_selection.selection.id,
        "OPEN",
        _B_HEAD,
    )
    assert {(row[1], row[2]) for row in decisions} == {
        (DecisionOutcome.APPROVED.value, DecisionValidity.INVALIDATED.value),
        (DecisionOutcome.CHANGES_REQUESTED.value, DecisionValidity.INVALIDATED.value),
    }
    initial_b = next(
        item for item in initial_evidence.work_items if item.work_item_id == b_work_item_id
    )
    second_b = next(
        item for item in second_evidence.work_items if item.work_item_id == b_work_item_id
    )
    assert initial_b.task_commit_sha == _B_HEAD
    assert second_b.task_commit_sha == _B_HEAD


def test_negative_review_waits_for_sibling_formal_create_callback_then_retries(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    accepted, _, dependencies, a_work_item_id, b_work_item_id = _accepted_two_work_item_requirement(
        isolated_requirement_database
    )
    routing = _formal_routing()

    with isolated_requirement_database.runtime.begin() as db:
        b_requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=accepted.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-create-race-b-request",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_ready = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=b_work_item_id,
            binding_id=_B_FORMAL_BINDING,
            head_sha=_B_HEAD,
            expected_revision=b_requested.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-create-race-b-ready",
            correlation_id="v06-formal-create-race-b-ready",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        a_requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=a_work_item_id,
            expected_revision=b_ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-create-race-a-request",
            dependencies=dependencies,
        )

    with pytest.raises(FormalDeliveryConflict, match="cycle is busy"):
        with isolated_requirement_database.runtime.begin() as db:
            decide_formal_review(
                SqlAlchemyRequirementRepository(db),
                requirement_id=accepted.requirement.id,
                gate_id=b_ready.gate.id,
                outcome=DecisionOutcome.CHANGES_REQUESTED,
                reason="B must wait while A's create callback is in flight.",
                expected_revision=a_requested.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key="v06-formal-create-race-b-negative",
                dependencies=dependencies,
            )

    with isolated_requirement_database.owner.connect() as db:
        rolled_back = db.execute(
            text(
                "SELECT gate.state, selection.invalidated_at, "
                "(SELECT count(*) FROM requirement.delivery_decision "
                "WHERE gate_id=:gate_id) AS decision_count "
                "FROM requirement.delivery_gate AS gate "
                "JOIN requirement.integration_baseline_selection AS selection "
                "ON selection.id=gate.selection_id WHERE gate.id=:gate_id"
            ),
            {"gate_id": b_ready.gate.id},
        ).one()
    assert tuple(rolled_back) == ("OPEN", None, 0)

    with isolated_requirement_database.runtime.begin() as db:
        a_ready = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=a_work_item_id,
            binding_id=_A_FORMAL_BINDING,
            head_sha=_A_INITIAL_HEAD,
            expected_revision=a_requested.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-create-race-a-ready",
            correlation_id="v06-formal-create-race-a-ready",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        retried = decide_formal_review(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            gate_id=b_ready.gate.id,
            outcome=DecisionOutcome.CHANGES_REQUESTED,
            reason="B must wait while A's create callback is in flight.",
            expected_revision=a_ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-create-race-b-negative",
            dependencies=dependencies,
        )

    assert retried.requirement.state is RequirementState.IN_PROGRESS
    assert retried.decision.validity is DecisionValidity.INVALIDATED


def test_negative_review_waits_for_sibling_merge_then_recovers_partial_completion(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    accepted, _, dependencies, a_work_item_id, b_work_item_id = _accepted_two_work_item_requirement(
        isolated_requirement_database
    )
    routing = _formal_routing()

    with isolated_requirement_database.runtime.begin() as db:
        a_requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=a_work_item_id,
            expected_revision=accepted.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-a-request",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        a_ready = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=a_work_item_id,
            binding_id=_A_FORMAL_BINDING,
            head_sha=_A_INITIAL_HEAD,
            expected_revision=a_requested.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-merge-race-a-ready",
            correlation_id="v06-formal-merge-race-a-ready",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        a_approved = decide_formal_review(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            gate_id=a_ready.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="A is approved for its exact head.",
            expected_revision=a_ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-a-review",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        a_merge_requested = request_formal_merge(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=a_work_item_id,
            expected_revision=a_approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-a-merge",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=a_merge_requested.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-b-request",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_ready = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=b_work_item_id,
            binding_id=_B_FORMAL_BINDING,
            head_sha=_B_HEAD,
            expected_revision=b_requested.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-merge-race-b-ready",
            correlation_id="v06-formal-merge-race-b-ready",
            dependencies=dependencies,
        )

    with pytest.raises(FormalDeliveryConflict, match="cycle is busy"):
        with isolated_requirement_database.runtime.begin() as db:
            decide_formal_review(
                SqlAlchemyRequirementRepository(db),
                requirement_id=accepted.requirement.id,
                gate_id=b_ready.gate.id,
                outcome=DecisionOutcome.CHANGES_REQUESTED,
                reason="B must wait while A's merge callback is in flight.",
                expected_revision=b_ready.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key="v06-formal-merge-race-b-negative",
                dependencies=dependencies,
            )

    with isolated_requirement_database.runtime.begin() as db:
        a_merged = record_formal_merged(
            SqlAlchemyRequirementRepository(db),
            work_item_id=a_work_item_id,
            binding_id=_A_FORMAL_BINDING,
            head_sha=_A_INITIAL_HEAD,
            merge_commit_sha="1" * 40,
            expected_revision=a_merge_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-merge-race-a-merged",
            correlation_id="v06-formal-merge-race-a-merged",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_rejected = decide_formal_review(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            gate_id=b_ready.gate.id,
            outcome=DecisionOutcome.CHANGES_REQUESTED,
            reason="B must wait while A's merge callback is in flight.",
            expected_revision=a_merged.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-b-negative",
            dependencies=dependencies,
        )

    with isolated_requirement_database.owner.connect() as db:
        partial = db.execute(
            text(
                "SELECT state, integration_delivery_state, formal_delivery_state, "
                "formal_merge_request_binding_id::text FROM requirement.work_item "
                "WHERE id=:work_item_id"
            ),
            {"work_item_id": a_work_item_id},
        ).one()
    assert tuple(partial) == (
        WorkItemState.COMPLETED.value,
        IntegrationDeliveryState.INTEGRATED.value,
        FormalDeliveryState.MERGED.value,
        _A_FORMAL_BINDING,
    )

    with isolated_requirement_database.runtime.begin() as db:
        b_integration_requested = request_integration_merge_request(
            db,
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=b_rejected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-b-integration-request",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_integration_ready = record_integration_mr_ready(
            db,
            work_item_id=b_work_item_id,
            binding_id=_B_REWORK_INTEGRATION_BINDING,
            expected_revision=b_integration_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-merge-race-b-integration-ready",
            correlation_id="v06-formal-merge-race-b-integration-ready",
            dependencies=dependencies,
        )
    assert b_integration_ready.requirement.state is RequirementState.VERIFYING
    with isolated_requirement_database.runtime.begin() as db:
        b_integration_merge = request_integration_merge(
            db,
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=b_integration_ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-b-integration-merge",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_reintegrated = record_integration_merged(
            db,
            work_item_id=b_work_item_id,
            binding_id=_B_REWORK_INTEGRATION_BINDING,
            expected_revision=b_integration_merge.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-merge-race-b-integration-merged",
            correlation_id="v06-formal-merge-race-b-integration-merged",
            dependencies=dependencies,
        )

    with isolated_requirement_database.runtime.begin() as db:
        snapshot = request_integration_baseline(
            db,
            requirement_id=accepted.requirement.id,
            expected_revision=b_reintegrated.requirement.revision,
            expected_requirement_version=b_reintegrated.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-baseline-v2",
            dependencies=dependencies,
        )
    evidence = _evidence(
        evidence_id="95000000-0000-0000-0000-000000000703",
        evidence_hash_character="8",
        snapshot=snapshot.snapshot,
        requirement_id=accepted.requirement.id,
        a_work_item_id=a_work_item_id,
        a_head=_A_INITIAL_HEAD,
        b_work_item_id=b_work_item_id,
    )
    refreshed_dependencies = replace(
        dependencies,
        integration_evidence=StaticEvidence(evidence),
    )
    with isolated_requirement_database.runtime.begin() as db:
        selection = select_integration_baseline(
            db,
            requirement_id=accepted.requirement.id,
            delivery_snapshot_id=snapshot.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=snapshot.requirement.revision,
            expected_requirement_version=snapshot.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-selection-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        acceptance = confirm_requirement_acceptance(
            db,
            requirement_id=accepted.requirement.id,
            selection_id=selection.selection.id,
            expected_revision=selection.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-acceptance-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        approved = decide_requirement_acceptance(
            db,
            requirement_id=accepted.requirement.id,
            gate_id=acceptance.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="The refreshed partial-completion evidence is accepted.",
            expected_revision=acceptance.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-approve-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_refreshed = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-b-formal-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_second_gate = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=b_work_item_id,
            binding_id=_B_FORMAL_BINDING,
            head_sha=_B_HEAD,
            expected_revision=b_refreshed.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-merge-race-b-ready-v2",
            correlation_id="v06-formal-merge-race-b-ready-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_second_approved = decide_formal_review(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            gate_id=b_second_gate.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="B's refreshed cycle is approved.",
            expected_revision=b_second_gate.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-b-review-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_second_merge = request_formal_merge(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=b_second_approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge-race-b-merge-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        completed = record_formal_merged(
            SqlAlchemyRequirementRepository(db),
            work_item_id=b_work_item_id,
            binding_id=_B_FORMAL_BINDING,
            head_sha=_B_HEAD,
            merge_commit_sha="2" * 40,
            expected_revision=b_second_merge.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-merge-race-b-merged-v2",
            correlation_id="v06-formal-merge-race-b-merged-v2",
            dependencies=refreshed_dependencies,
        )

    assert completed.requirement.state is RequirementState.COMPLETED
    with isolated_requirement_database.owner.connect() as db:
        final_states = db.execute(
            text(
                "SELECT state, formal_delivery_state FROM requirement.work_item "
                "WHERE requirement_id=:requirement_id ORDER BY id"
            ),
            {"requirement_id": accepted.requirement.id},
        ).all()
    assert [tuple(row) for row in final_states] == [
        (WorkItemState.COMPLETED.value, FormalDeliveryState.MERGED.value),
        (WorkItemState.COMPLETED.value, FormalDeliveryState.MERGED.value),
    ]


def test_two_invalidating_create_blocks_ack_then_finalize_one_aggregate_cycle(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    accepted, _, dependencies, a_work_item_id, b_work_item_id = _accepted_two_work_item_requirement(
        isolated_requirement_database
    )
    with isolated_requirement_database.runtime.begin() as db:
        a_requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=a_work_item_id,
            expected_revision=accepted.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-double-create-block-a-request",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        b_requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=a_requested.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-double-create-block-b-request",
            dependencies=dependencies,
        )

    with isolated_requirement_database.runtime.begin() as db:
        first_block = record_formal_delivery_blocked(
            SqlAlchemyRequirementRepository(db),
            work_item_id=a_work_item_id,
            binding_id=None,
            reason_code=FormalDeliveryBlockedReason.HEAD_SHA_CHANGED,
            expected_revision=a_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-double-create-block-a",
            correlation_id="v06-double-create-block-a",
            dependencies=dependencies,
        )
    assert first_block.work_item.formal_delivery_state is FormalDeliveryState.BLOCKED
    assert (
        first_block.work_item.formal_blocked_reason_code
        == FormalDeliveryBlockedReason.HEAD_SHA_CHANGED.value
    )
    assert first_block.requirement.current_integration_baseline_selection_id is not None
    with pytest.raises(FormalDeliveryBlocked, match="invalidation is pending"):
        with isolated_requirement_database.runtime.begin() as db:
            request_formal_merge_request(
                SqlAlchemyRequirementRepository(db),
                requirement_id=accepted.requirement.id,
                work_item_id=a_work_item_id,
                expected_revision=first_block.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key="v06-double-create-block-old-cycle-retry",
                dependencies=dependencies,
            )

    with isolated_requirement_database.runtime.begin() as db:
        finalized = record_formal_delivery_blocked(
            SqlAlchemyRequirementRepository(db),
            work_item_id=b_work_item_id,
            binding_id=None,
            reason_code=FormalDeliveryBlockedReason.HEAD_SHA_CHANGED,
            expected_revision=b_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-double-create-block-b",
            correlation_id="v06-double-create-block-b",
            dependencies=dependencies,
        )

    assert finalized.requirement.state is RequirementState.IN_PROGRESS
    assert finalized.requirement.current_integration_baseline_selection_id is None
    with isolated_requirement_database.owner.connect() as db:
        work_items = db.execute(
            text(
                "SELECT state, integration_delivery_state, formal_delivery_state, "
                "formal_blocked_reason_code FROM requirement.work_item "
                "WHERE requirement_id=:requirement_id ORDER BY id"
            ),
            {"requirement_id": accepted.requirement.id},
        ).all()
        selection_invalidated = db.execute(
            text(
                "SELECT invalidated_at IS NOT NULL "
                "FROM requirement.integration_baseline_selection WHERE id=:selection_id"
            ),
            {"selection_id": accepted.selection.id},
        ).scalar_one()
    assert [tuple(row) for row in work_items] == [
        (
            WorkItemState.IN_PROGRESS.value,
            IntegrationDeliveryState.IMPLEMENTING.value,
            FormalDeliveryState.NOT_STARTED.value,
            None,
        ),
        (
            WorkItemState.IN_PROGRESS.value,
            IntegrationDeliveryState.IMPLEMENTING.value,
            FormalDeliveryState.NOT_STARTED.value,
            None,
        ),
    ]
    assert selection_invalidated is True


def test_two_invalidating_merge_blocks_preserve_bindings_and_converge(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    accepted, _, dependencies, a_work_item_id, b_work_item_id = _accepted_two_work_item_requirement(
        isolated_requirement_database
    )
    routing = _formal_routing()

    def open_and_approve(
        work_item_id: str,
        binding_id: str,
        head_sha: str,
        expected_revision: int,
        suffix: str,
    ) -> tuple[FormalDeliveryCommandResult, AcceptanceDecisionResult]:
        with isolated_requirement_database.runtime.begin() as db:
            requested = request_formal_merge_request(
                SqlAlchemyRequirementRepository(db),
                requirement_id=accepted.requirement.id,
                work_item_id=work_item_id,
                expected_revision=expected_revision,
                actor=Actor("employee-1"),
                idempotency_key=f"v06-double-merge-{suffix}-create",
                dependencies=dependencies,
            )
        with isolated_requirement_database.runtime.begin() as db:
            ready = record_formal_mr_ready(
                SqlAlchemyRequirementRepository(db),
                work_item_id=work_item_id,
                binding_id=binding_id,
                head_sha=head_sha,
                expected_revision=requested.work_item.revision,
                assignment=routing,
                actor=Actor("SYSTEM:SOURCE_CONTROL"),
                idempotency_key=f"v06-double-merge-{suffix}-ready",
                correlation_id=f"v06-double-merge-{suffix}-ready",
                dependencies=dependencies,
            )
        with isolated_requirement_database.runtime.begin() as db:
            approved = decide_formal_review(
                SqlAlchemyRequirementRepository(db),
                requirement_id=accepted.requirement.id,
                gate_id=ready.gate.id,
                outcome=DecisionOutcome.APPROVED,
                reason=f"{suffix} is approved for its exact head.",
                expected_revision=ready.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key=f"v06-double-merge-{suffix}-review",
                dependencies=dependencies,
            )
        return requested, approved

    a_requested, a_approved = open_and_approve(
        a_work_item_id,
        _A_FORMAL_BINDING,
        _A_INITIAL_HEAD,
        accepted.requirement.revision,
        "a",
    )
    with isolated_requirement_database.runtime.begin() as db:
        a_merge = request_formal_merge(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=a_work_item_id,
            expected_revision=a_approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-double-merge-a-request",
            dependencies=dependencies,
        )
    _b_requested, b_approved = open_and_approve(
        b_work_item_id,
        _B_FORMAL_BINDING,
        _B_HEAD,
        a_merge.requirement.revision,
        "b",
    )
    with isolated_requirement_database.runtime.begin() as db:
        b_merge = request_formal_merge(
            SqlAlchemyRequirementRepository(db),
            requirement_id=accepted.requirement.id,
            work_item_id=b_work_item_id,
            expected_revision=b_approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-double-merge-b-request",
            dependencies=dependencies,
        )

    with isolated_requirement_database.runtime.begin() as db:
        first_block = record_formal_delivery_blocked(
            SqlAlchemyRequirementRepository(db),
            work_item_id=a_work_item_id,
            binding_id=_A_FORMAL_BINDING,
            reason_code=FormalDeliveryBlockedReason.HEAD_SHA_CHANGED,
            expected_revision=a_merge.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-double-merge-a-block",
            correlation_id="v06-double-merge-a-block",
            dependencies=dependencies,
        )
    assert first_block.work_item.formal_delivery_state is FormalDeliveryState.BLOCKED
    assert first_block.work_item.formal_merge_request_binding_id == _A_FORMAL_BINDING

    with isolated_requirement_database.runtime.begin() as db:
        finalized = record_formal_delivery_blocked(
            SqlAlchemyRequirementRepository(db),
            work_item_id=b_work_item_id,
            binding_id=_B_FORMAL_BINDING,
            reason_code=FormalDeliveryBlockedReason.HEAD_SHA_CHANGED,
            expected_revision=b_merge.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-double-merge-b-block",
            correlation_id="v06-double-merge-b-block",
            dependencies=dependencies,
        )

    assert finalized.requirement.state is RequirementState.IN_PROGRESS
    with isolated_requirement_database.owner.connect() as db:
        work_items = db.execute(
            text(
                "SELECT id::text, state, integration_delivery_state, formal_delivery_state, "
                "formal_merge_request_binding_id::text, formal_blocked_reason_code "
                "FROM requirement.work_item WHERE requirement_id=:requirement_id "
                "ORDER BY id"
            ),
            {"requirement_id": accepted.requirement.id},
        ).all()
    work_items_by_id = {row[0]: tuple(row[1:]) for row in work_items}
    assert work_items_by_id == {
        a_work_item_id: (
            WorkItemState.IN_PROGRESS.value,
            IntegrationDeliveryState.IMPLEMENTING.value,
            FormalDeliveryState.MR_OPEN.value,
            _A_FORMAL_BINDING,
            None,
        ),
        b_work_item_id: (
            WorkItemState.IN_PROGRESS.value,
            IntegrationDeliveryState.IMPLEMENTING.value,
            FormalDeliveryState.MR_OPEN.value,
            _B_FORMAL_BINDING,
            None,
        ),
    }
