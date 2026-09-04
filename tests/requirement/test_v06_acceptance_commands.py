from dataclasses import asdict, dataclass, replace
from typing import TypedDict

import pytest
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    ArtifactEvidenceReference,
    DecisionOutcome,
    DeliveryGatePolicySnapshot,
    DeliveryReviewerEligibilitySnapshot,
    EvidenceUnavailableOrStale,
    GateReviewerMismatch,
    IntegrationBaselineEvidenceSnapshot,
    IntegrationBaselineEvidenceWorkItem,
    RequestIntegrationBaselineResult,
    RequirementDependencies,
    RequirementState,
    SelectionStale,
    confirm_requirement_acceptance,
    decide_requirement_acceptance,
    get_current_acceptance_proof,
    request_integration_baseline,
    request_integration_merge_request,
    select_integration_baseline,
    submit_external_validation,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.delivery_policy_helpers import resolved_policy
from tests.requirement.test_baseline_gate import ARTIFACT_HASH_V1, _gate_dependencies
from tests.requirement.test_commands import Actor
from tests.requirement.test_v06_evidence_commands import _integrated_requirement


class _SelectionCommand(TypedDict):
    requirement_id: str
    delivery_snapshot_id: str
    integration_baseline_id: str
    expected_revision: int
    expected_requirement_version: int
    actor: Actor
    idempotency_key: str
    dependencies: RequirementDependencies


@dataclass(frozen=True, slots=True)
class StaticEvidence:
    snapshot: IntegrationBaselineEvidenceSnapshot

    def get(self, evidence_id: str) -> IntegrationBaselineEvidenceSnapshot:
        assert evidence_id == self.snapshot.id
        return self.snapshot

    def get_by_snapshot(
        self, *, delivery_snapshot_id: str, delivery_snapshot_hash: str
    ) -> IntegrationBaselineEvidenceSnapshot:
        assert delivery_snapshot_id == self.snapshot.delivery_snapshot_id
        assert delivery_snapshot_hash == self.snapshot.delivery_snapshot_hash
        return self.snapshot


@dataclass(frozen=True, slots=True)
class StaticDeliveryPolicies:
    def requirement_acceptance(
        self,
        *,
        workspace_id: str,
        requirement_created_by: str,
    ) -> DeliveryGatePolicySnapshot:
        return DeliveryGatePolicySnapshot(
            version=3,
            default_reviewer_id=requirement_created_by,
            policy_code="REQUIREMENT_ACCEPTANCE_CREATOR",
            snapshot_hash="sha256:" + resolved_policy(3).snapshot_hash,
            resolution_snapshot={
                "workspaceId": workspace_id,
                "rule": "REQUIREMENT_CREATOR",
                "policy": asdict(resolved_policy(3)),
            },
        )


@dataclass(frozen=True, slots=True)
class StaticDeliveryReviewerGuard:
    eligible: bool = True

    def evaluate(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        required_capabilities: tuple[str, ...],
    ) -> DeliveryReviewerEligibilitySnapshot:
        return DeliveryReviewerEligibilitySnapshot(
            eligible=self.eligible,
            actor_id=actor_id,
            required_capabilities=required_capabilities,
            workspace_id=workspace_id,
            account_version=2,
            workspace_version=11,
            principal_version=5,
            snapshot_hash="sha256:" + ("8" if self.eligible else "9") * 64,
            details={"actorId": actor_id, "membership": "ACTIVE"},
        )


def _selection_fixture(
    database: IsolatedRequirementDatabase,
    *,
    key_suffix: str,
) -> tuple[
    RequestIntegrationBaselineResult,
    IntegrationBaselineEvidenceSnapshot,
    RequirementDependencies,
]:
    requirement_id, work_item_id, revision, requirement_version = _integrated_requirement(
        database,
        key_suffix=key_suffix,
    )
    base_dependencies = _gate_dependencies()
    with database.runtime.begin() as db:
        requested = request_integration_baseline(
            db,
            requirement_id=requirement_id,
            expected_revision=revision,
            expected_requirement_version=requirement_version,
            actor=Actor("employee-1"),
            idempotency_key=f"{key_suffix}-snapshot",
            dependencies=base_dependencies,
        )
    evidence = IntegrationBaselineEvidenceSnapshot(
        id=f"95000000-0000-0000-0000-{len(key_suffix):012d}",
        evidence_hash="sha256:" + "6" * 64,
        delivery_snapshot_id=requested.snapshot.id,
        delivery_snapshot_hash=requested.snapshot.snapshot_hash,
        requirement_id=requirement_id,
        requirement_version=requirement_version,
        required_work_item_set_version=requested.snapshot.required_work_item_set_version,
        required_work_item_set_hash=requested.snapshot.required_work_item_set_hash,
        currentness_state="CURRENT",
        currentness_reasons=(),
        work_items=(
            IntegrationBaselineEvidenceWorkItem(
                work_item_id=work_item_id,
                repository_id="repository-1",
                task_commit_sha="b" * 40,
                integration_merge_commit_sha="c" * 40,
                artifact_references=(
                    ArtifactEvidenceReference(
                        artifact_id="sdd-1",
                        artifact_version="version-1",
                        artifact_hash=ARTIFACT_HASH_V1,
                    ),
                ),
            ),
        ),
        generated_at=base_dependencies.clock.now(),
    )
    dependencies = replace(
        base_dependencies,
        integration_evidence=StaticEvidence(evidence),
        delivery_gate_policies=StaticDeliveryPolicies(),
        delivery_reviewer_guard=StaticDeliveryReviewerGuard(),
    )
    return requested, evidence, dependencies


def test_selection_is_exact_versioned_and_idempotent(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix="v06-selection",
    )
    command: _SelectionCommand = {
        "requirement_id": requested.requirement.id,
        "delivery_snapshot_id": requested.snapshot.id,
        "integration_baseline_id": evidence.id,
        "expected_revision": requested.requirement.revision,
        "expected_requirement_version": requested.requirement.requirement_version,
        "actor": Actor("employee-1"),
        "idempotency_key": "v06-selection",
        "dependencies": dependencies,
    }
    with isolated_requirement_database.runtime.begin() as db:
        first = select_integration_baseline(db, **command)
    with isolated_requirement_database.runtime.begin() as db:
        replay = select_integration_baseline(db, **command)

    assert replay == first
    assert first.requirement.state is RequirementState.AWAITING_ACCEPTANCE
    assert first.requirement.requirement_version == evidence.requirement_version + 1
    assert first.selection.integration_baseline_hash == evidence.evidence_hash
    assert first.requirement.current_integration_baseline_selection_id == first.selection.id
    with isolated_requirement_database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT requirement_version_before, requirement_version_after, "
                "invalidated_at FROM requirement.integration_baseline_selection "
                "WHERE id=:selection_id"
            ),
            {"selection_id": first.selection.id},
        ).one()
        idempotent_status = db.execute(
            text(
                "SELECT http_status FROM requirement.idempotency_record "
                "WHERE idempotency_key='v06-selection'"
            )
        ).scalar_one()
    assert row == (evidence.requirement_version, evidence.requirement_version + 1, None)
    assert idempotent_status == 200


def test_selection_rejects_evidence_with_a_stale_snapshot_binding(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix="v06-selection-stale",
    )
    dependencies = replace(
        dependencies,
        integration_evidence=StaticEvidence(
            evidence.model_copy(update={"delivery_snapshot_hash": "sha256:" + "0" * 64})
        ),
    )
    with pytest.raises(SelectionStale, match="snapshot"):
        with isolated_requirement_database.runtime.begin() as db:
            select_integration_baseline(
                db,
                requirement_id=requested.requirement.id,
                delivery_snapshot_id=requested.snapshot.id,
                integration_baseline_id=evidence.id,
                expected_revision=requested.requirement.revision,
                expected_requirement_version=requested.requirement.requirement_version,
                actor=Actor("employee-1"),
                idempotency_key="v06-selection-stale",
                dependencies=dependencies,
            )


def test_selection_rejects_evidence_with_a_stale_source_control_proof(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix="v06-selection-currentness",
    )
    dependencies = replace(
        dependencies,
        integration_evidence=StaticEvidence(
            evidence.model_copy(
                update={
                    "currentness_state": "STALE",
                    "currentness_reasons": ("OBSERVATION_HEAD_CHANGED",),
                }
            )
        ),
    )

    with pytest.raises(EvidenceUnavailableOrStale, match="Evidence is stale"):
        with isolated_requirement_database.runtime.begin() as db:
            select_integration_baseline(
                db,
                requirement_id=requested.requirement.id,
                delivery_snapshot_id=requested.snapshot.id,
                integration_baseline_id=evidence.id,
                expected_revision=requested.requirement.revision,
                expected_requirement_version=requested.requirement.requirement_version,
                actor=Actor("employee-1"),
                idempotency_key="v06-selection-currentness",
                dependencies=dependencies,
            )


def test_selection_distinguishes_an_unavailable_source_control_proof(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix="v06-selection-unavailable",
    )
    dependencies = replace(
        dependencies,
        integration_evidence=StaticEvidence(
            evidence.model_copy(
                update={
                    "currentness_state": "UNAVAILABLE",
                    "currentness_reasons": ("OBSERVATION_UNAVAILABLE",),
                }
            )
        ),
    )

    with pytest.raises(EvidenceUnavailableOrStale, match="proof is unavailable"):
        with isolated_requirement_database.runtime.begin() as db:
            select_integration_baseline(
                db,
                requirement_id=requested.requirement.id,
                delivery_snapshot_id=requested.snapshot.id,
                integration_baseline_id=evidence.id,
                expected_revision=requested.requirement.revision,
                expected_requirement_version=requested.requirement.requirement_version,
                actor=Actor("employee-1"),
                idempotency_key="v06-selection-unavailable",
                dependencies=dependencies,
            )


def test_acceptance_requires_current_assignee_and_live_eligibility_then_approves(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix="v06-acceptance",
    )
    with isolated_requirement_database.runtime.begin() as db:
        selected = select_integration_baseline(
            db,
            requirement_id=requested.requirement.id,
            delivery_snapshot_id=requested.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=requested.requirement.revision,
            expected_requirement_version=requested.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-acceptance-selection",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = confirm_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            selection_id=selected.selection.id,
            expected_revision=selected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-acceptance-confirm",
            dependencies=dependencies,
        )
    with pytest.raises(GateReviewerMismatch):
        with isolated_requirement_database.runtime.begin() as db:
            decide_requirement_acceptance(
                db,
                requirement_id=selected.requirement.id,
                gate_id=confirmation.gate.id,
                outcome=DecisionOutcome.APPROVED,
                reason="Exact Evidence meets every criterion.",
                expected_revision=confirmation.requirement.revision,
                actor=Actor("employee-2"),
                idempotency_key="v06-acceptance-decide",
                dependencies=dependencies,
            )
    with isolated_requirement_database.runtime.begin() as db:
        decided = decide_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="Exact Evidence meets every criterion.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-acceptance-decide",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.connect() as db:
        proof = get_current_acceptance_proof(
            db,
            requirement_id=selected.requirement.id,
            dependencies=dependencies,
        )
    with isolated_requirement_database.owner.connect() as db:
        confirmation_status = db.execute(
            text(
                "SELECT http_status FROM requirement.idempotency_record "
                "WHERE idempotency_key='v06-acceptance-confirm'"
            )
        ).scalar_one()

    assert confirmation.assignment.current_reviewer_id == "employee-1"
    assert decided.requirement.state is RequirementState.AWAITING_MERGE
    assert decided.requirement.requirement_version == selected.requirement.requirement_version
    assert proof.current is True
    assert proof.outcome is DecisionOutcome.APPROVED
    assert proof.integration_baseline_id == evidence.id
    assert confirmation_status == 200


@pytest.mark.parametrize("phase", ["confirm", "decide"])
@pytest.mark.parametrize("currentness_state", ["STALE", "UNAVAILABLE"])
def test_acceptance_consumers_fail_closed_when_selected_evidence_is_no_longer_current(
    isolated_requirement_database: IsolatedRequirementDatabase,
    phase: str,
    currentness_state: str,
) -> None:
    suffix = f"v06-acceptance-{phase}-{currentness_state.lower()}"
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix=suffix,
    )
    with isolated_requirement_database.runtime.begin() as db:
        selected = select_integration_baseline(
            db,
            requirement_id=requested.requirement.id,
            delivery_snapshot_id=requested.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=requested.requirement.revision,
            expected_requirement_version=requested.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key=f"{suffix}-selection",
            dependencies=dependencies,
        )
    confirmation = None
    if phase == "decide":
        with isolated_requirement_database.runtime.begin() as db:
            confirmation = confirm_requirement_acceptance(
                db,
                requirement_id=selected.requirement.id,
                selection_id=selected.selection.id,
                expected_revision=selected.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key=f"{suffix}-confirm",
                dependencies=dependencies,
            )
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
            if phase == "confirm":
                confirm_requirement_acceptance(
                    db,
                    requirement_id=selected.requirement.id,
                    selection_id=selected.selection.id,
                    expected_revision=selected.requirement.revision,
                    actor=Actor("employee-1"),
                    idempotency_key=f"{suffix}-confirm",
                    dependencies=changed_dependencies,
                )
            else:
                assert confirmation is not None
                decide_requirement_acceptance(
                    db,
                    requirement_id=selected.requirement.id,
                    gate_id=confirmation.gate.id,
                    outcome=DecisionOutcome.APPROVED,
                    reason="The selected evidence remains current.",
                    expected_revision=confirmation.requirement.revision,
                    actor=Actor("employee-1"),
                    idempotency_key=f"{suffix}-decide",
                    dependencies=changed_dependencies,
                )


def test_new_external_validation_invalidates_approved_acceptance(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix="v06-invalidate",
    )
    with isolated_requirement_database.runtime.begin() as db:
        selected = select_integration_baseline(
            db,
            requirement_id=requested.requirement.id,
            delivery_snapshot_id=requested.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=requested.requirement.revision,
            expected_requirement_version=requested.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-invalidate-selection",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = confirm_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            selection_id=selected.selection.id,
            expected_revision=selected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-invalidate-confirm",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        approved = decide_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="Evidence accepted.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-invalidate-decide",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        changed = submit_external_validation(
            db,
            requirement_id=selected.requirement.id,
            work_item_id=evidence.work_items[0].work_item_id,
            target_commit_sha="b" * 40,
            integration_merge_commit_sha="c" * 40,
            reference="urn:jenkins:platform:43",
            notes="A newer exact validation run supersedes the accepted input.",
            artifact_references=evidence.work_items[0].artifact_references,
            expected_revision=approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-invalidate-new-validation",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.connect() as db:
        proof = get_current_acceptance_proof(
            db,
            requirement_id=selected.requirement.id,
            dependencies=dependencies,
        )

    assert changed.requirement.state is RequirementState.VERIFYING
    assert changed.requirement.requirement_version == approved.requirement.requirement_version + 1
    assert changed.requirement.current_integration_baseline_selection_id is None
    assert changed.requirement.current_acceptance_gate_id is None
    assert proof.current is False


@pytest.mark.parametrize(
    "outcome",
    [DecisionOutcome.CHANGES_REQUESTED, DecisionOutcome.REJECTED],
)
def test_non_approved_acceptance_returns_to_work_and_invalidates_delivery_subject(
    isolated_requirement_database: IsolatedRequirementDatabase,
    outcome: DecisionOutcome,
) -> None:
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix=f"v06-{outcome.value.lower()}",
    )
    with isolated_requirement_database.runtime.begin() as db:
        selected = select_integration_baseline(
            db,
            requirement_id=requested.requirement.id,
            delivery_snapshot_id=requested.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=requested.requirement.revision,
            expected_requirement_version=requested.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key=f"v06-{outcome.value}-selection",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = confirm_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            selection_id=selected.selection.id,
            expected_revision=selected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"v06-{outcome.value}-confirm",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        decided = decide_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=outcome,
            reason="The evidence needs correction.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"v06-{outcome.value}-decide",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.connect() as db:
        proof = get_current_acceptance_proof(
            db,
            requirement_id=selected.requirement.id,
            dependencies=dependencies,
        )

    assert decided.requirement.state is RequirementState.IN_PROGRESS
    assert decided.requirement.current_integration_baseline_selection_id is None
    assert decided.requirement.current_acceptance_gate_id is None
    assert decided.selection.invalidated_at is not None
    assert decided.gate.state.value == "INVALIDATED"
    assert decided.decision.validity.value == "INVALIDATED"
    assert proof.current is False
    with isolated_requirement_database.owner.connect() as db:
        rework_state = db.execute(
            text(
                "SELECT state, integration_delivery_state, "
                "integration_merge_request_binding_id FROM requirement.work_item "
                "WHERE id=:work_item_id"
            ),
            {"work_item_id": evidence.work_items[0].work_item_id},
        ).one()
    assert rework_state == ("IN_PROGRESS", "IMPLEMENTING", None)
    with isolated_requirement_database.runtime.begin() as db:
        rework = request_integration_merge_request(
            db,
            requirement_id=decided.requirement.id,
            work_item_id=evidence.work_items[0].work_item_id,
            expected_revision=decided.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"v06-{outcome.value}-reintegration",
            dependencies=dependencies,
        )
    assert rework.work_item.integration_delivery_state.value == "MR_PENDING"
