from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.app.modules.requirement.application.acceptance import (
    _require_current_selected_evidence,
    _validate_selection_subject,
)
from control_plane.app.modules.requirement.application.delivery_queries import (
    get_delivery_snapshot_evidence,
)
from control_plane.app.modules.requirement.application.formal import _current_evidence_item
from control_plane.app.modules.requirement.domain import (
    ArtifactEvidenceReference,
    EvidenceUnavailableOrStale,
    RequirementDeliverySnapshot,
    SelectionStale,
)
from control_plane.app.modules.requirement.domain.delivery import required_work_item_set_hash
from control_plane.app.modules.requirement.ports import (
    ArtifactSnapshot,
    ArtifactState,
    ArtifactTrust,
    IntegrationBaselineEvidenceSnapshot,
    IntegrationBaselineEvidenceWorkItem,
)


@pytest.fixture
def context() -> Any:
    snapshot = RequirementDeliverySnapshot.create(
        snapshot_id="snapshot-1",
        requirement_id="requirement-1",
        requirement_version=7,
        required_work_item_set_version=3,
        required_work_item_set_hash=required_work_item_set_hash(("work-1",)),
        work_item_ids=("work-1",),
        created_by="employee-1",
    ).model_dump()
    artifact = ArtifactSnapshot(
        id="artifact-1",
        version="1",
        sha256="sha256:" + "a" * 64,
        state=ArtifactState.AVAILABLE,
        media_type="text/plain",
        trust=ArtifactTrust.TRUSTED_PLAIN_TEXT,
    )
    evidence = IntegrationBaselineEvidenceSnapshot(
        id="evidence-1",
        evidence_hash="sha256:" + "b" * 64,
        delivery_snapshot_id=snapshot["id"],
        delivery_snapshot_hash=snapshot["snapshot_hash"],
        requirement_id=snapshot["requirement_id"],
        requirement_version=snapshot["requirement_version"],
        required_work_item_set_version=snapshot["required_work_item_set_version"],
        required_work_item_set_hash=snapshot["required_work_item_set_hash"],
        currentness_state="CURRENT",
        currentness_reasons=(),
        work_items=(
            IntegrationBaselineEvidenceWorkItem(
                work_item_id="work-1",
                repository_id="repository-1",
                task_commit_sha="c" * 40,
                integration_merge_commit_sha="d" * 40,
                artifact_references=(
                    ArtifactEvidenceReference(
                        artifact_id=artifact.id,
                        artifact_version=artifact.version,
                        artifact_hash=artifact.sha256,
                    ),
                ),
            ),
        ),
        generated_at=datetime(2026, 9, 29, tzinfo=UTC),
    )
    subject = SimpleNamespace(
        snapshot=snapshot,
        artifact=artifact,
        evidence=evidence,
        requirement={
            "id": "requirement-1",
            "state": "VERIFYING",
            "requirement_version": 7,
            "required_work_item_set_version": 3,
            "required_work_item_set_hash": snapshot["required_work_item_set_hash"],
            "current_integration_baseline_selection_id": None,
        },
        selection=None,
        work_items=[{"id": "work-1", "repository_id": "repository-1"}],
    )
    subject.repository = SimpleNamespace(
        requirement_by_id=lambda _: subject.requirement,
        delivery_snapshot_by_id=lambda _: subject.snapshot,
        integration_baseline_selection_by_id=lambda _: subject.selection,
        work_items=lambda _: subject.work_items,
    )
    subject.dependencies = SimpleNamespace(
        integration_evidence=SimpleNamespace(
            get=lambda _: subject.evidence, get_by_snapshot=lambda **_: subject.evidence
        ),
        artifacts=SimpleNamespace(get_snapshot=lambda *_: subject.artifact),
    )
    return subject


@pytest.mark.parametrize(
    "change", [{"state": ArtifactState.UNAVAILABLE}, {"sha256": "sha256:" + "e" * 64}]
)
def test_selection_revalidates_exact_artifact_at_decision_time(context: Any, change: Any) -> None:
    context.artifact = context.artifact.model_copy(update=change)
    with pytest.raises(EvidenceUnavailableOrStale):
        _validate_selection_subject(
            context.repository,
            requirement=context.requirement,
            delivery_snapshot_id="snapshot-1",
            integration_baseline_id="evidence-1",
            expected_requirement_version=7,
            dependencies=context.dependencies,
        )


def test_selection_rejects_an_uncovered_current_work_item(context: Any) -> None:
    context.work_items.append({"id": "work-2", "repository_id": "repository-1"})
    with pytest.raises(SelectionStale):
        _validate_selection_subject(
            context.repository,
            requirement=context.requirement,
            delivery_snapshot_id="snapshot-1",
            integration_baseline_id="evidence-1",
            expected_requirement_version=7,
            dependencies=context.dependencies,
        )


@pytest.mark.parametrize("selected", [False, True])
def test_read_distinguishes_changed_input_from_the_selection_version_bump(
    context: Any, selected: bool
) -> None:
    context.requirement["requirement_version"] = 8
    if selected:
        context.requirement["current_integration_baseline_selection_id"] = "selection-1"
        context.selection = {
            "delivery_snapshot_id": "snapshot-1",
            "integration_baseline_id": "evidence-1",
            "integration_baseline_hash": context.evidence.evidence_hash,
            "requirement_version_before": 7,
            "requirement_version_after": 8,
            "invalidated_at": None,
        }
    result = get_delivery_snapshot_evidence(
        context.repository,
        requirement_id="requirement-1",
        snapshot_id="snapshot-1",
        dependencies=context.dependencies,
    )
    assert result.currentness_state == ("CURRENT" if selected else "STALE")
    assert result.currentness_reasons == (() if selected else ("REQUIREMENT_INPUT_CHANGED",))


def test_read_exposes_unavailable_artifact_proof(context: Any) -> None:
    context.artifact = context.artifact.model_copy(update={"state": ArtifactState.UNAVAILABLE})
    result = get_delivery_snapshot_evidence(
        context.repository,
        requirement_id="requirement-1",
        snapshot_id="snapshot-1",
        dependencies=context.dependencies,
    )
    assert result.currentness_state == "UNAVAILABLE"
    assert result.currentness_reasons == ("ARTIFACT_UNAVAILABLE_OR_STALE",)


@pytest.mark.parametrize("consumer", ["acceptance", "formal"])
@pytest.mark.parametrize(
    "change", [{"state": ArtifactState.UNAVAILABLE}, {"sha256": "sha256:" + "e" * 64}]
)
def test_acceptance_and_formal_consumers_recheck_the_selected_artifact(
    context: Any, consumer: str, change: Any
) -> None:
    context.artifact = context.artifact.model_copy(update=change)
    subject = {
        "requirement_id": "requirement-1",
        "requirement_state": "AWAITING_MERGE",
        "acceptance_decision_id": "decision-1",
        "acceptance_outcome": "APPROVED",
        "acceptance_validity": "CURRENT",
        "integration_baseline_id": context.evidence.id,
        "integration_baseline_hash": context.evidence.evidence_hash,
        "work_item_id": "work-1",
    }
    with pytest.raises(EvidenceUnavailableOrStale):
        if consumer == "acceptance":
            _require_current_selected_evidence(subject, dependencies=context.dependencies)
        else:
            _current_evidence_item(subject, context.dependencies)
