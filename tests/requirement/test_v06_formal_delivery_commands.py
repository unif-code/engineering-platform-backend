from dataclasses import replace

import pytest
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    AcceptanceDecisionResult,
    EvidenceUnavailableOrStale,
    IntegrationBaselineEvidenceSnapshot,
    RequirementDependencies,
    confirm_requirement_acceptance,
    decide_requirement_acceptance,
    record_integration_merged,
    record_integration_mr_ready,
    request_integration_baseline,
    request_integration_merge,
    request_integration_merge_request,
    select_integration_baseline,
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
    DecisionOutcome,
    DecisionValidity,
    FormalDeliveryBlockedReason,
    FormalDeliveryState,
    RequirementState,
    WorkItemState,
)
from control_plane.app.modules.requirement.ports import DeliveryGatePolicySnapshot
from control_plane.app.shared.idempotency import IdempotencyConflict
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.delivery_policy_helpers import frozen_policy, resolved_policy
from tests.requirement.test_commands import Actor
from tests.requirement.test_v06_acceptance_commands import StaticEvidence, _selection_fixture


def _approved_requirement(
    database: IsolatedRequirementDatabase,
) -> tuple[
    AcceptanceDecisionResult,
    IntegrationBaselineEvidenceSnapshot,
    RequirementDependencies,
]:
    requested, evidence, dependencies = _selection_fixture(
        database,
        key_suffix="v06-formal",
    )
    with database.runtime.begin() as db:
        selected = select_integration_baseline(
            db,
            requirement_id=requested.requirement.id,
            delivery_snapshot_id=requested.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=requested.requirement.revision,
            expected_requirement_version=requested.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-select",
            dependencies=dependencies,
        )
    with database.runtime.begin() as db:
        opened = confirm_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            selection_id=selected.selection.id,
            expected_revision=selected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-acceptance-open",
            dependencies=dependencies,
        )
    with database.runtime.begin() as db:
        approved = decide_requirement_acceptance(
            db,
            requirement_id=opened.requirement.id,
            gate_id=opened.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="Exact evidence is accepted.",
            expected_revision=opened.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-acceptance-approve",
            dependencies=dependencies,
        )
    return approved, evidence, replace(dependencies)


def test_formal_delivery_is_exact_idempotent_and_completes_after_reviewed_merge(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    approved, evidence, dependencies = _approved_requirement(isolated_requirement_database)
    work_item_id = evidence.work_items[0].work_item_id
    binding_id = "96000000-0000-0000-0000-000000000601"
    head_sha = evidence.work_items[0].task_commit_sha

    create_command = dict(
        requirement_id=approved.requirement.id,
        work_item_id=work_item_id,
        expected_revision=approved.requirement.revision,
        actor=Actor("employee-1"),
        idempotency_key="v06-formal-create",
        dependencies=dependencies,
    )
    with isolated_requirement_database.runtime.begin() as db:
        requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db), **create_command
        )
    with isolated_requirement_database.runtime.begin() as db:
        replayed = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db), **create_command
        )

    assert replayed == requested
    assert requested.work_item.state is WorkItemState.AWAITING_MERGE
    assert requested.work_item.formal_delivery_state is FormalDeliveryState.MR_PENDING

    routing = DeliveryGatePolicySnapshot(
        version=4,
        default_reviewer_id="employee-1",
        policy_code="FORMAL_REVIEW_WORK_ITEM_OWNER",
        snapshot_hash="sha256:" + resolved_policy(4).snapshot_hash,
        resolution_snapshot=frozen_policy(),
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
            idempotency_key="v06-formal-ready",
            correlation_id="formal-ready-1",
            dependencies=dependencies,
        )
    assert ready.work_item.formal_delivery_state is FormalDeliveryState.MR_OPEN
    assert ready.gate.subject_head_sha == head_sha

    with isolated_requirement_database.runtime.begin() as db:
        reviewed = decide_formal_review(
            SqlAlchemyRequirementRepository(db),
            requirement_id=approved.requirement.id,
            gate_id=ready.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="The exact formal diff is approved.",
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-review",
            dependencies=dependencies,
        )

    with isolated_requirement_database.runtime.begin() as db:
        merge_requested = request_formal_merge(
            SqlAlchemyRequirementRepository(db),
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=reviewed.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-merge",
            dependencies=dependencies,
        )
    assert merge_requested.work_item.formal_delivery_state is FormalDeliveryState.MERGE_PENDING

    with isolated_requirement_database.runtime.begin() as db:
        completed = record_formal_merged(
            SqlAlchemyRequirementRepository(db),
            work_item_id=work_item_id,
            binding_id=binding_id,
            head_sha=head_sha,
            merge_commit_sha="d" * 40,
            expected_revision=merge_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-merged",
            correlation_id="formal-merged-1",
            dependencies=dependencies,
        )

    assert completed.work_item.state is WorkItemState.COMPLETED
    assert completed.work_item.formal_delivery_state is FormalDeliveryState.MERGED
    assert completed.requirement.state is RequirementState.COMPLETED


def test_formal_mr_ready_idempotency_fingerprint_binds_the_complete_assignment(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    approved, evidence, dependencies = _approved_requirement(isolated_requirement_database)
    work_item_id = evidence.work_items[0].work_item_id
    with isolated_requirement_database.runtime.begin() as db:
        requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-assignment-fingerprint-create",
            dependencies=dependencies,
        )
    routing = DeliveryGatePolicySnapshot(
        version=4,
        default_reviewer_id="employee-1",
        policy_code="FORMAL_REVIEW_WORK_ITEM_OWNER",
        snapshot_hash="sha256:" + resolved_policy(4).snapshot_hash,
        resolution_snapshot=frozen_policy(),
    )

    def record(assignment: DeliveryGatePolicySnapshot) -> None:
        with isolated_requirement_database.runtime.begin() as db:
            record_formal_mr_ready(
                SqlAlchemyRequirementRepository(db),
                work_item_id=work_item_id,
                binding_id="96000000-0000-0000-0000-000000000602",
                head_sha=evidence.work_items[0].task_commit_sha,
                assignment=assignment,
                expected_revision=requested.work_item.revision,
                actor=Actor("SYSTEM:SOURCE_CONTROL"),
                idempotency_key="v06-formal-assignment-fingerprint-ready",
                correlation_id="formal-assignment-fingerprint-ready",
                dependencies=dependencies,
            )

    record(routing)

    changed_assignment = routing.model_copy(
        update={"resolution_snapshot": {"rule": "WORK_ITEM_OWNER", "revision": 2}}
    )
    with pytest.raises(IdempotencyConflict):
        record(changed_assignment)


@pytest.mark.parametrize("currentness_state", ["STALE", "UNAVAILABLE"])
def test_formal_delivery_fails_closed_when_selected_evidence_is_no_longer_current(
    isolated_requirement_database: IsolatedRequirementDatabase,
    currentness_state: str,
) -> None:
    approved, evidence, dependencies = _approved_requirement(isolated_requirement_database)
    changed_dependencies = replace(
        dependencies,
        integration_evidence=StaticEvidence(
            evidence.model_copy(
                update={
                    "currentness_state": currentness_state,
                    "currentness_reasons": ("OBSERVATION_HEAD_CHANGED",),
                }
            )
        ),
    )
    expected_message = (
        "proof is unavailable" if currentness_state == "UNAVAILABLE" else "Evidence is stale"
    )

    with pytest.raises(EvidenceUnavailableOrStale, match=expected_message):
        with isolated_requirement_database.runtime.begin() as db:
            request_formal_merge_request(
                SqlAlchemyRequirementRepository(db),
                requirement_id=approved.requirement.id,
                work_item_id=evidence.work_items[0].work_item_id,
                expected_revision=approved.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key=f"v06-formal-currentness-{currentness_state.lower()}",
                dependencies=changed_dependencies,
            )


@pytest.mark.parametrize("head_changed", [True, False], ids=["new-head", "same-head"])
def test_negative_formal_review_preserves_history_and_allows_new_selection_review_cycle(
    isolated_requirement_database: IsolatedRequirementDatabase,
    head_changed: bool,
) -> None:
    approved, evidence, dependencies = _approved_requirement(isolated_requirement_database)
    work_item_id = evidence.work_items[0].work_item_id
    binding_id = "96000000-0000-0000-0000-000000000611"
    original_head = evidence.work_items[0].task_commit_sha
    routing = DeliveryGatePolicySnapshot(
        version=4,
        default_reviewer_id="employee-1",
        policy_code="FORMAL_REVIEW_WORK_ITEM_OWNER",
        snapshot_hash="sha256:" + resolved_policy(4).snapshot_hash,
        resolution_snapshot=frozen_policy(),
    )

    with isolated_requirement_database.runtime.begin() as db:
        requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-negative-create",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        ready = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=work_item_id,
            binding_id=binding_id,
            head_sha=original_head,
            expected_revision=requested.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-negative-ready",
            correlation_id="formal-negative-ready",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        rejected = decide_formal_review(
            SqlAlchemyRequirementRepository(db),
            requirement_id=approved.requirement.id,
            gate_id=ready.gate.id,
            outcome=DecisionOutcome.CHANGES_REQUESTED,
            reason="Update the implementation and return with a new exact head.",
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-negative-review",
            dependencies=dependencies,
        )

    assert rejected.requirement.state is RequirementState.IN_PROGRESS
    assert rejected.requirement.current_integration_baseline_selection_id is None
    assert rejected.decision.validity is DecisionValidity.INVALIDATED
    with isolated_requirement_database.owner.connect() as db:
        work_item = db.execute(
            text(
                "SELECT state, formal_delivery_state, "
                "formal_merge_request_binding_id::text, integration_delivery_state, "
                "integration_merge_request_binding_id::text "
                "FROM requirement.work_item WHERE id=:work_item_id"
            ),
            {"work_item_id": work_item_id},
        ).one()
    assert work_item == (
        WorkItemState.IN_PROGRESS.value,
        FormalDeliveryState.MR_OPEN.value,
        binding_id,
        "IMPLEMENTING",
        None,
    )
    new_integration_binding_id = "96000000-0000-0000-0000-000000000612"
    with isolated_requirement_database.runtime.begin() as db:
        rework_requested = request_integration_merge_request(
            db,
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=rejected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-negative-integration-create-v2",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        rework_ready = record_integration_mr_ready(
            db,
            work_item_id=work_item_id,
            binding_id=new_integration_binding_id,
            expected_revision=rework_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-negative-integration-ready-v2",
            correlation_id="formal-negative-integration-ready-v2",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        rework_merge_requested = request_integration_merge(
            db,
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=rework_ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-negative-integration-merge-v2",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        rework_merged = record_integration_merged(
            db,
            work_item_id=work_item_id,
            binding_id=new_integration_binding_id,
            expected_revision=rework_merge_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-negative-integration-merged-v2",
            correlation_id="formal-negative-integration-merged-v2",
            dependencies=dependencies,
        )

    # Rebuild exact evidence and Acceptance only after the public re-integration chain.
    with isolated_requirement_database.runtime.begin() as db:
        snapshot = request_integration_baseline(
            db,
            requirement_id=approved.requirement.id,
            expected_revision=rework_merged.requirement.revision,
            expected_requirement_version=rework_merged.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-negative-snapshot-v2",
            dependencies=dependencies,
        )
    new_head = "e" * 40 if head_changed else original_head
    refreshed_evidence = evidence.model_copy(
        update={
            "id": "95000000-0000-0000-0000-000000000611",
            "evidence_hash": "sha256:" + "e" * 64,
            "delivery_snapshot_id": snapshot.snapshot.id,
            "delivery_snapshot_hash": snapshot.snapshot.snapshot_hash,
            "requirement_version": snapshot.requirement.requirement_version,
            "work_items": (
                evidence.work_items[0].model_copy(update={"task_commit_sha": new_head}),
            ),
        }
    )
    refreshed_dependencies = replace(
        dependencies,
        integration_evidence=StaticEvidence(refreshed_evidence),
    )
    with isolated_requirement_database.runtime.begin() as db:
        selected = select_integration_baseline(
            db,
            requirement_id=approved.requirement.id,
            delivery_snapshot_id=snapshot.snapshot.id,
            integration_baseline_id=refreshed_evidence.id,
            expected_revision=snapshot.requirement.revision,
            expected_requirement_version=snapshot.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-negative-select-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        acceptance = confirm_requirement_acceptance(
            db,
            requirement_id=approved.requirement.id,
            selection_id=selected.selection.id,
            expected_revision=selected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-negative-acceptance-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        accepted = decide_requirement_acceptance(
            db,
            requirement_id=approved.requirement.id,
            gate_id=acceptance.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="The replacement exact-head evidence is accepted.",
            expected_revision=acceptance.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-negative-approve-v2",
            dependencies=refreshed_dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        refreshed_request = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=accepted.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-negative-create-v2",
            dependencies=refreshed_dependencies,
        )

    assert refreshed_request.work_item.formal_delivery_state is FormalDeliveryState.MR_PENDING
    assert refreshed_request.work_item.formal_merge_request_binding_id == binding_id
    with isolated_requirement_database.runtime.begin() as db:
        second_gate = record_formal_mr_ready(
            SqlAlchemyRequirementRepository(db),
            work_item_id=work_item_id,
            binding_id=binding_id,
            head_sha=new_head,
            expected_revision=refreshed_request.work_item.revision,
            assignment=routing,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-negative-ready-v2",
            correlation_id="formal-negative-ready-v2",
            dependencies=refreshed_dependencies,
        )
    assert second_gate.gate.id != ready.gate.id
    assert second_gate.gate.selection_id != ready.gate.selection_id
    assert second_gate.gate.subject_head_sha == new_head
    with isolated_requirement_database.owner.connect() as db:
        gate_count = db.execute(
            text(
                "SELECT count(*) FROM requirement.delivery_gate "
                "WHERE formal_merge_request_binding_id=:binding_id"
            ),
            {"binding_id": binding_id},
        ).scalar_one()
    assert gate_count == 2


def test_formal_head_drift_callback_invalidates_acceptance_and_restores_development_state(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    approved, evidence, dependencies = _approved_requirement(isolated_requirement_database)
    work_item_id = evidence.work_items[0].work_item_id
    with isolated_requirement_database.runtime.begin() as db:
        requested = request_formal_merge_request(
            SqlAlchemyRequirementRepository(db),
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-drift-create",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        blocked = record_formal_delivery_blocked(
            SqlAlchemyRequirementRepository(db),
            work_item_id=work_item_id,
            binding_id=None,
            reason_code=FormalDeliveryBlockedReason.HEAD_SHA_CHANGED,
            expected_revision=requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-drift-blocked",
            correlation_id="formal-drift-blocked",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = record_formal_delivery_blocked(
            SqlAlchemyRequirementRepository(db),
            work_item_id=work_item_id,
            binding_id=None,
            reason_code=FormalDeliveryBlockedReason.HEAD_SHA_CHANGED,
            expected_revision=requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-formal-drift-blocked",
            correlation_id="formal-drift-blocked",
            dependencies=dependencies,
        )

    assert replay == blocked
    assert blocked.reason_code is FormalDeliveryBlockedReason.HEAD_SHA_CHANGED
    assert blocked.requirement.state is RequirementState.IN_PROGRESS
    assert blocked.requirement.current_integration_baseline_selection_id is None
    assert blocked.work_item.state is WorkItemState.IN_PROGRESS
    assert blocked.work_item.formal_delivery_state is FormalDeliveryState.NOT_STARTED
    assert blocked.work_item.formal_merge_request_binding_id is None
    assert blocked.work_item.integration_delivery_state.value == "IMPLEMENTING"
    assert blocked.work_item.integration_merge_request_binding_id is None
    with isolated_requirement_database.runtime.begin() as db:
        rework = request_integration_merge_request(
            db,
            requirement_id=blocked.requirement.id,
            work_item_id=work_item_id,
            expected_revision=blocked.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-formal-drift-reintegration",
            dependencies=dependencies,
        )
    assert rework.work_item.integration_delivery_state.value == "MR_PENDING"
