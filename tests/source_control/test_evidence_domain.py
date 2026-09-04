from typing import TypedDict

from control_plane.app.modules.source_control.domain.evidence import (
    ArtifactReference,
    ExternalValidationReference,
    IntegrationBaselineEvidenceItem,
    canonical_integration_baseline_hash,
)
from control_plane.app.shared.security import sanitize_external_reference


class _IntegrationBaselineHashArguments(TypedDict):
    integration_baseline_id: str
    requirement_id: str
    requirement_version: int
    required_work_item_set_version: int
    required_work_item_set_hash: str
    delivery_snapshot_id: str
    delivery_snapshot_hash: str


def _item(*, work_item_id: str, target_commit_sha: str) -> IntegrationBaselineEvidenceItem:
    validation = ExternalValidationReference(
        id=f"validation-{work_item_id}",
        work_item_id=work_item_id,
        integration_merge_request_binding_id=f"binding-{work_item_id}",
        target_commit_sha=target_commit_sha,
        integration_merge_commit_sha="b" * 40,
        reference="https://jenkins.example.test/job/platform/42",
        notes="Manually verified the exact integration commit.",
        artifact_references=(
            ArtifactReference(
                artifact_id="30000000-0000-0000-0000-000000000601",
                artifact_version="2",
                artifact_hash="sha256:" + "c" * 64,
            ),
        ),
        submitted_by="employee-1",
    )
    return IntegrationBaselineEvidenceItem(
        work_item_id=work_item_id,
        repository_id=f"repository-{work_item_id[-1]}",
        task_branch=f"feat/wi-{work_item_id[-1]}-delivery",
        task_commit_sha=target_commit_sha,
        integration_merge_request_binding_id=f"binding-{work_item_id}",
        integration_merge_request_iid=int(work_item_id[-1]),
        integration_merge_commit_sha="b" * 40,
        executor_type="HUMAN",
        executor_id="employee-1",
        artifact_references=validation.artifact_references,
        external_validation=validation,
    )


def test_external_reference_drops_query_and_fragment_credentials() -> None:
    assert sanitize_external_reference("https://jenkins.example.test") == (
        "https://jenkins.example.test"
    )
    assert sanitize_external_reference("https://jenkins.example.test/") == (
        "https://jenkins.example.test/"
    )
    assert (
        sanitize_external_reference(
            "https://jenkins.example.test/job/platform/42?token=do-not-store#console"
        )
        == "https://jenkins.example.test/job/platform/42"
    )
    assert sanitize_external_reference("urn:jenkins:platform:42") == "urn:jenkins:platform:42"
    assert (
        sanitize_external_reference("urn:jenkins:platform:42?token=do-not-store#console")
        == "urn:jenkins:platform:42"
    )


def test_evidence_hash_is_stable_by_work_item_order_and_binds_every_field() -> None:
    first_item = _item(
        work_item_id="20000000-0000-0000-0000-000000000601",
        target_commit_sha="a" * 40,
    )
    second_item = _item(
        work_item_id="20000000-0000-0000-0000-000000000602",
        target_commit_sha="d" * 40,
    )
    values: _IntegrationBaselineHashArguments = {
        "integration_baseline_id": "40000000-0000-0000-0000-000000000601",
        "requirement_id": "10000000-0000-0000-0000-000000000601",
        "requirement_version": 7,
        "required_work_item_set_version": 3,
        "required_work_item_set_hash": "sha256:" + "e" * 64,
        "delivery_snapshot_id": "50000000-0000-0000-0000-000000000601",
        "delivery_snapshot_hash": "sha256:" + "f" * 64,
    }

    first = canonical_integration_baseline_hash(items=(second_item, first_item), **values)
    reordered = canonical_integration_baseline_hash(items=(first_item, second_item), **values)
    changed = canonical_integration_baseline_hash(
        items=(
            _item(
                work_item_id=first_item.work_item_id,
                target_commit_sha="9" * 40,
            ),
            second_item,
        ),
        **values,
    )

    assert first == reordered
    assert first.startswith("sha256:")
    assert first != changed
