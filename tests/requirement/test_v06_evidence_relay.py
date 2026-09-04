from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import Connection

from control_plane.app.modules.requirement import (
    ExternalValidationRequestMessage,
    IntegrationBaselineRequestMessage,
    RequirementDependencies,
    RequirementError,
    claim_evidence_requests,
)

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
LEASE_UNTIL = NOW + timedelta(minutes=1)
REQUIREMENT_ID = "10000000-0000-0000-0000-000000000601"
WORK_ITEM_ID = "20000000-0000-0000-0000-000000000601"
ARTIFACT_HASH = "sha256:" + "a" * 64
DELIVERY_SNAPSHOT_HASH = "sha256:" + "b" * 64
REQUIRED_SET_HASH = "sha256:" + "c" * 64


class _EvidenceRepository:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def claim_evidence_requests(
        self,
        *,
        limit: int,
        available_before: datetime,
        lease_until: datetime,
    ) -> list[dict[str, object]]:
        assert (limit, available_before, lease_until) == (1, NOW, LEASE_UNTIL)
        return self._rows


@dataclass(frozen=True, slots=True)
class _Dependencies:
    repository: _EvidenceRepository

    def repository_factory(self, db: Connection) -> _EvidenceRepository:
        del db
        return self.repository


def _outbox_row(
    *,
    message_id: str,
    topic: str,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "id": message_id,
        "topic": topic,
        "aggregate_type": "REQUIREMENT",
        "aggregate_id": REQUIREMENT_ID,
        "aggregate_version": 9,
        "payload": payload,
        "state": "PENDING",
        "attempts": 1,
        "available_at": LEASE_UNTIL,
        "created_at": NOW,
        "published_at": None,
        "last_error_code": None,
    }


def _claim(
    row: dict[str, object],
) -> tuple[ExternalValidationRequestMessage | IntegrationBaselineRequestMessage, ...]:
    repository = _EvidenceRepository([row])
    dependencies = cast(RequirementDependencies, _Dependencies(repository))
    return claim_evidence_requests(
        cast(Connection, object()),
        limit=1,
        available_before=NOW,
        lease_until=LEASE_UNTIL,
        dependencies=dependencies,
    )


def _external_validation_payload() -> dict[str, object]:
    return {
        "artifactReferences": [
            {
                "artifact_id": "artifact-601",
                "artifact_version": "version-1",
                "artifact_hash": ARTIFACT_HASH,
            }
        ],
        "integrationMergeCommitSha": "c" * 40,
        "integrationMergeRequestBindingId": "30000000-0000-0000-0000-000000000601",
        "notes": "Validated against the exact integration commit.",
        "reference": "https://jenkins.example.test/job/platform/601",
        "repositoryId": "repository-601",
        "requirementId": REQUIREMENT_ID,
        "requirementVersion": 8,
        "submittedBy": "employee-601",
        "targetCommitSha": "b" * 40,
        "workItemId": WORK_ITEM_ID,
        "workItemRevision": 6,
    }


def test_claim_maps_real_external_validation_outbox_payload_to_domain_message() -> None:
    message_id = "40000000-0000-0000-0000-000000000601"
    payload = _external_validation_payload()

    (message,) = _claim(
        _outbox_row(
            message_id=message_id,
            topic="requirement.external-validation.submitted",
            payload=payload,
        )
    )

    assert isinstance(message, ExternalValidationRequestMessage)
    assert message.model_dump(exclude={"payload_hash"}) == {
        "message_id": message_id,
        "requirement_id": REQUIREMENT_ID,
        "requirement_version": 8,
        "work_item_id": WORK_ITEM_ID,
        "work_item_revision": 6,
        "repository_id": "repository-601",
        "integration_merge_request_binding_id": "30000000-0000-0000-0000-000000000601",
        "target_commit_sha": "b" * 40,
        "integration_merge_commit_sha": "c" * 40,
        "reference": "https://jenkins.example.test/job/platform/601",
        "notes": "Validated against the exact integration commit.",
        "artifact_references": (
            {
                "artifact_id": "artifact-601",
                "artifact_version": "version-1",
                "artifact_hash": ARTIFACT_HASH,
            },
        ),
        "submitted_by": "employee-601",
        "submitted_at": NOW,
        "attempts": 1,
    }
    assert message.payload_hash.startswith("sha256:")


def test_claim_maps_real_integration_baseline_outbox_payload_to_domain_message() -> None:
    message_id = "40000000-0000-0000-0000-000000000602"
    snapshot_id = "50000000-0000-0000-0000-000000000601"
    payload: dict[str, object] = {
        "deliverySnapshotHash": DELIVERY_SNAPSHOT_HASH,
        "deliverySnapshotId": snapshot_id,
        "requestedBy": "employee-601",
        "requiredWorkItemSetHash": REQUIRED_SET_HASH,
        "requiredWorkItemSetVersion": 4,
        "requirementId": REQUIREMENT_ID,
        "requirementVersion": 8,
        "workItemIds": [WORK_ITEM_ID],
    }

    (message,) = _claim(
        _outbox_row(
            message_id=message_id,
            topic="requirement.integration-baseline.requested",
            payload=payload,
        )
    )

    assert isinstance(message, IntegrationBaselineRequestMessage)
    assert message.model_dump(exclude={"payload_hash"}) == {
        "message_id": message_id,
        "delivery_snapshot_id": snapshot_id,
        "delivery_snapshot_hash": DELIVERY_SNAPSHOT_HASH,
        "requirement_id": REQUIREMENT_ID,
        "requirement_version": 8,
        "required_work_item_set_version": 4,
        "required_work_item_set_hash": REQUIRED_SET_HASH,
        "work_item_ids": (WORK_ITEM_ID,),
        "requested_by": "employee-601",
        "attempts": 1,
    }
    assert message.payload_hash.startswith("sha256:")


def test_claim_rejects_malformed_artifact_shape_without_exposing_payload() -> None:
    message_id = "40000000-0000-0000-0000-000000000603"
    secret_marker = "provider-credential-must-not-leak"
    payload = _external_validation_payload()
    payload["artifactReferences"] = [
        {
            "artifact_id": "artifact-601",
            "artifact_version": "version-1",
            "artifact_hash": secret_marker,
        }
    ]

    with pytest.raises(RequirementError) as raised:
        _claim(
            _outbox_row(
                message_id=message_id,
                topic="requirement.external-validation.submitted",
                payload=payload,
            )
        )

    assert raised.value.args == (message_id,)
    assert secret_marker not in str(raised.value)
    assert secret_marker not in repr(raised.value)
