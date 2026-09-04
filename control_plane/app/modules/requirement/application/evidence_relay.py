import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    ArtifactEvidenceReference,
    ExternalValidationRequestMessage,
    IntegrationBaselineRequestMessage,
    InvalidRequirementInput,
    RequirementError,
)
from control_plane.app.modules.requirement.ports import RequirementRepository

_EXTERNAL_TOPIC = "requirement.external-validation.submitted"
_BASELINE_TOPIC = "requirement.integration-baseline.requested"
_TOPICS = frozenset({_EXTERNAL_TOPIC, _BASELINE_TOPIC})
_RELEASE_ERROR_CODES = frozenset(
    {
        "EVIDENCE_REQUEST_CONFLICT",
        "EVIDENCE_REQUEST_INVALID",
        "SOURCE_CONTROL_UNAVAILABLE",
    }
)


class EvidenceMessageInvalid(RequirementError):
    pass


class EvidenceRequestMissing(RequirementError):
    pass


def _payload_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _external_message(row: Any, payload: dict[str, object]) -> ExternalValidationRequestMessage:
    expected_fields = {
        "artifactReferences",
        "integrationMergeCommitSha",
        "integrationMergeRequestBindingId",
        "notes",
        "reference",
        "repositoryId",
        "requirementId",
        "requirementVersion",
        "submittedBy",
        "targetCommitSha",
        "workItemId",
        "workItemRevision",
    }
    if set(payload) != expected_fields:
        raise EvidenceMessageInvalid(str(row["id"]))
    try:
        raw_artifacts = payload["artifactReferences"]
        if not isinstance(raw_artifacts, list):
            raise EvidenceMessageInvalid(str(row["id"]))
        artifacts = tuple(ArtifactEvidenceReference.model_validate(item) for item in raw_artifacts)
        return ExternalValidationRequestMessage.model_validate(
            {
                "message_id": str(row["id"]),
                "payload_hash": _payload_hash(payload),
                "requirement_id": payload["requirementId"],
                "requirement_version": payload["requirementVersion"],
                "work_item_id": payload["workItemId"],
                "work_item_revision": payload["workItemRevision"],
                "repository_id": payload["repositoryId"],
                "integration_merge_request_binding_id": payload["integrationMergeRequestBindingId"],
                "target_commit_sha": payload["targetCommitSha"],
                "integration_merge_commit_sha": payload["integrationMergeCommitSha"],
                "reference": payload["reference"],
                "notes": payload["notes"],
                "artifact_references": artifacts,
                "submitted_by": payload["submittedBy"],
                "submitted_at": row["created_at"],
                "attempts": row["attempts"],
            }
        )
    except (TypeError, ValidationError, ValueError):
        raise EvidenceMessageInvalid(str(row["id"])) from None


def _baseline_message(row: Any, payload: dict[str, object]) -> IntegrationBaselineRequestMessage:
    expected_fields = {
        "deliverySnapshotHash",
        "deliverySnapshotId",
        "requestedBy",
        "requiredWorkItemSetHash",
        "requiredWorkItemSetVersion",
        "requirementId",
        "requirementVersion",
        "workItemIds",
    }
    if set(payload) != expected_fields:
        raise EvidenceMessageInvalid(str(row["id"]))
    try:
        return IntegrationBaselineRequestMessage.model_validate(
            {
                "message_id": str(row["id"]),
                "payload_hash": _payload_hash(payload),
                "delivery_snapshot_id": payload["deliverySnapshotId"],
                "delivery_snapshot_hash": payload["deliverySnapshotHash"],
                "requirement_id": payload["requirementId"],
                "requirement_version": payload["requirementVersion"],
                "required_work_item_set_version": payload["requiredWorkItemSetVersion"],
                "required_work_item_set_hash": payload["requiredWorkItemSetHash"],
                "work_item_ids": payload["workItemIds"],
                "requested_by": payload["requestedBy"],
                "attempts": row["attempts"],
            }
        )
    except (TypeError, ValidationError, ValueError):
        raise EvidenceMessageInvalid(str(row["id"])) from None


def _message(
    row: Any,
) -> ExternalValidationRequestMessage | IntegrationBaselineRequestMessage:
    payload = row["payload"]
    if (
        row["aggregate_type"] != "REQUIREMENT"
        or row["topic"] not in _TOPICS
        or not isinstance(payload, dict)
        or payload.get("requirementId") != str(row["aggregate_id"])
    ):
        raise EvidenceMessageInvalid(str(row["id"]))
    if row["topic"] == _EXTERNAL_TOPIC:
        return _external_message(row, payload)
    return _baseline_message(row, payload)


def claim_evidence_requests(
    repository: RequirementRepository,
    *,
    limit: int,
    available_before: datetime,
    lease_until: datetime,
) -> tuple[ExternalValidationRequestMessage | IntegrationBaselineRequestMessage, ...]:
    if not 1 <= limit <= 100 or lease_until <= available_before:
        raise InvalidRequirementInput("Evidence request lease is invalid")
    return tuple(
        _message(row)
        for row in repository.claim_evidence_requests(
            limit=limit,
            available_before=available_before,
            lease_until=lease_until,
        )
    )


def acknowledge_evidence_request(
    repository: RequirementRepository,
    *,
    message_id: str,
    consumer: str,
    dependencies: RequirementDependencies,
) -> None:
    if consumer != "SOURCE_CONTROL":
        raise InvalidRequirementInput("Evidence request consumer is invalid")
    row = repository.outbox_by_id(message_id, for_update=True)
    if row is None or row["topic"] not in _TOPICS:
        raise EvidenceRequestMissing(message_id)
    _message(row)
    if row["state"] == "PUBLISHED":
        return
    if repository.publish_outbox(message_id, now=dependencies.clock.now()) is None:
        raise EvidenceRequestMissing(message_id)


def release_evidence_request(
    repository: RequirementRepository,
    *,
    message_id: str,
    error_code: str,
    available_at: datetime,
) -> None:
    if error_code not in _RELEASE_ERROR_CODES:
        raise InvalidRequirementInput("Evidence request release error code is invalid")
    row = repository.outbox_by_id(message_id, for_update=True)
    if row is None or row["topic"] not in _TOPICS:
        raise EvidenceRequestMissing(message_id)
    _message(row)
    if row["state"] == "PUBLISHED":
        return
    if (
        repository.release_outbox(
            message_id,
            error_code=error_code,
            available_at=available_at,
        )
        is None
    ):
        raise EvidenceRequestMissing(message_id)
