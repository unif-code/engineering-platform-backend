import hashlib
import json
import re
from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.app.modules.requirement.domain.models import RequirementDto
from control_plane.app.modules.requirement.domain.transitions import RequirementError

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceCoverageConflict(ValueError):
    """The immutable evidence set does not exactly cover the required WorkItems."""


class DeliveryEvidenceConflict(RequirementError):
    """A Requirement cannot freeze or select the requested delivery evidence."""


class DeliverySnapshotConflict(DeliveryEvidenceConflict):
    """The Requirement cannot freeze the requested delivery snapshot."""


class EvidenceUnavailableOrStale(DeliveryEvidenceConflict):
    """The requested delivery evidence is unavailable or no longer current."""


def _canonical_hash(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _normalized_work_item_ids(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(value.strip() for value in values))
    if not normalized or any(not value for value in normalized):
        raise ValueError("at least one WorkItem is required")
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate WorkItem IDs are not allowed")
    return normalized


def canonical_acceptance_criteria_hash(criteria: Iterable[str]) -> str:
    normalized = tuple(value.strip() for value in criteria)
    if not normalized or any(not value for value in normalized):
        raise ValueError("acceptance criteria are required")
    return _canonical_hash({"acceptanceCriteria": list(normalized)})


def canonical_delivery_snapshot_hash(
    *,
    requirement_id: str,
    requirement_version: int,
    required_work_item_set_version: int,
    required_work_item_set_hash: str,
    work_item_ids: Iterable[str],
) -> str:
    stable_requirement_id = requirement_id.strip()
    if not stable_requirement_id:
        raise ValueError("requirement ID is required")
    if requirement_version < 1 or required_work_item_set_version < 1:
        raise ValueError("snapshot versions must be positive")
    if not _SHA256.fullmatch(required_work_item_set_hash):
        raise ValueError("required WorkItem set hash is invalid")
    normalized_ids = _normalized_work_item_ids(work_item_ids)
    return _canonical_hash(
        {
            "requirementId": stable_requirement_id,
            "requirementVersion": requirement_version,
            "requiredWorkItemSetHash": required_work_item_set_hash,
            "requiredWorkItemSetVersion": required_work_item_set_version,
            "workItemIds": list(normalized_ids),
        }
    )


def validate_evidence_coverage(
    *,
    required_work_item_ids: Iterable[str],
    evidence_work_item_ids: Iterable[str],
) -> tuple[str, ...]:
    try:
        required = _normalized_work_item_ids(required_work_item_ids)
    except ValueError as error:
        raise EvidenceCoverageConflict(str(error)) from error
    raw_evidence = tuple(value.strip() for value in evidence_work_item_ids)
    if len(set(raw_evidence)) != len(raw_evidence):
        raise EvidenceCoverageConflict("duplicate WorkItem evidence")
    evidence = tuple(sorted(raw_evidence))
    missing = sorted(set(required) - set(evidence))
    extra = sorted(set(evidence) - set(required))
    if missing:
        raise EvidenceCoverageConflict("missing WorkItem evidence: " + ",".join(missing))
    if extra:
        raise EvidenceCoverageConflict("extra WorkItem evidence: " + ",".join(extra))
    return evidence


class RequirementDeliverySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    requirement_id: str
    requirement_version: int = Field(ge=1)
    required_work_item_set_version: int = Field(ge=1)
    required_work_item_set_hash: str
    work_item_ids: tuple[str, ...]
    snapshot_hash: str
    created_by: str
    created_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        requirement_id: str,
        requirement_version: int,
        required_work_item_set_version: int,
        required_work_item_set_hash: str,
        work_item_ids: Iterable[str],
        created_by: str,
    ) -> "RequirementDeliverySnapshot":
        normalized_ids = _normalized_work_item_ids(work_item_ids)
        return cls(
            id=snapshot_id,
            requirement_id=requirement_id,
            requirement_version=requirement_version,
            required_work_item_set_version=required_work_item_set_version,
            required_work_item_set_hash=required_work_item_set_hash,
            work_item_ids=normalized_ids,
            snapshot_hash=canonical_delivery_snapshot_hash(
                requirement_id=requirement_id,
                requirement_version=requirement_version,
                required_work_item_set_version=required_work_item_set_version,
                required_work_item_set_hash=required_work_item_set_hash,
                work_item_ids=normalized_ids,
            ),
            created_by=created_by.strip(),
        )


class ArtifactEvidenceReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str
    artifact_version: str
    artifact_hash: str

    @field_validator("artifact_id", "artifact_version")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Artifact reference is required")
        return normalized

    @field_validator("artifact_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("Artifact hash must be canonical SHA-256")
        return value


class ExternalValidationSubmission(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    requirement_id: str
    requirement_version: int = Field(ge=1)
    work_item_id: str
    work_item_revision: int = Field(ge=1)
    repository_id: str
    integration_merge_request_binding_id: str
    target_commit_sha: str
    integration_merge_commit_sha: str
    reference: str
    notes: str
    artifact_references: tuple[ArtifactEvidenceReference, ...]
    submitted_by: str
    submitted_at: datetime


class SubmitExternalValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    submission: ExternalValidationSubmission
    outbox_topic: str


class RequestIntegrationBaselineResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    snapshot: RequirementDeliverySnapshot
    outbox_topic: str


class ExternalValidationRequestMessage(ExternalValidationSubmission):
    payload_hash: str
    attempts: int = Field(ge=1)

    @field_validator("payload_hash")
    @classmethod
    def validate_payload_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("payload hash must be canonical SHA-256")
        return value


class IntegrationBaselineRequestMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    payload_hash: str
    delivery_snapshot_id: str
    delivery_snapshot_hash: str
    requirement_id: str
    requirement_version: int = Field(ge=1)
    required_work_item_set_version: int = Field(ge=1)
    required_work_item_set_hash: str
    work_item_ids: tuple[str, ...]
    requested_by: str
    attempts: int = Field(ge=1)

    @field_validator(
        "payload_hash",
        "delivery_snapshot_hash",
        "required_work_item_set_hash",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("request hashes must be canonical SHA-256")
        return value
