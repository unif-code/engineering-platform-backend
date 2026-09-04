import hashlib
import json
import re
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.app.shared.security import sanitize_external_reference

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


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


class ArtifactReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str
    artifact_version: str
    artifact_hash: str

    @field_validator("artifact_id", "artifact_version")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Artifact reference values are required")
        return normalized

    @field_validator("artifact_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("Artifact hash must be canonical SHA-256")
        return value


class ExternalValidationReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    work_item_id: str
    integration_merge_request_binding_id: str
    target_commit_sha: str
    integration_merge_commit_sha: str
    reference: str
    notes: str
    artifact_references: tuple[ArtifactReference, ...] = ()
    submitted_by: str
    submitted_at: datetime | None = None

    @field_validator("target_commit_sha", "integration_merge_commit_sha")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("commit SHA must contain 40 lowercase hexadecimal characters")
        return value

    @field_validator("reference")
    @classmethod
    def validate_external_reference(cls, value: str) -> str:
        return sanitize_external_reference(value)

    @field_validator(
        "id",
        "work_item_id",
        "integration_merge_request_binding_id",
        "notes",
        "submitted_by",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("external validation fields are required")
        return normalized

    @model_validator(mode="after")
    def validate_artifact_set(self) -> "ExternalValidationReference":
        identities = [
            (item.artifact_id, item.artifact_version) for item in self.artifact_references
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate Artifact references are not allowed")
        return self


class ExternalValidationRequestEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    payload_hash: str
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
    artifact_references: tuple[ArtifactReference, ...]
    submitted_by: str
    submitted_at: datetime
    attempts: int = Field(ge=1)

    @field_validator("target_commit_sha", "integration_merge_commit_sha")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("commit SHA must contain 40 lowercase hexadecimal characters")
        return value

    @field_validator("payload_hash")
    @classmethod
    def validate_payload_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("payload hash must be canonical SHA-256")
        return value

    @field_validator("reference")
    @classmethod
    def validate_external_reference(cls, value: str) -> str:
        return sanitize_external_reference(value)

    @field_validator(
        "message_id",
        "requirement_id",
        "work_item_id",
        "repository_id",
        "integration_merge_request_binding_id",
        "notes",
        "submitted_by",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("external validation message fields are required")
        return normalized

    @model_validator(mode="after")
    def validate_artifact_set(self) -> "ExternalValidationRequestEnvelope":
        if not self.artifact_references:
            raise ValueError("external validation requires Artifact references")
        identities = tuple(
            (item.artifact_id, item.artifact_version) for item in self.artifact_references
        )
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate Artifact references are not allowed")
        return self

    @property
    def request_fingerprint(self) -> str:
        return _canonical_hash(self.model_dump(mode="json", exclude={"attempts"}))


class IntegrationBaselineRequestEnvelope(BaseModel):
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
            raise ValueError("Evidence request hashes must be canonical SHA-256")
        return value

    @field_validator(
        "message_id",
        "delivery_snapshot_id",
        "requirement_id",
        "requested_by",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Evidence request fields are required")
        return normalized

    @field_validator("work_item_ids")
    @classmethod
    def validate_work_item_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalized_work_item_ids(value)


class IntegrationBaselineEvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    repository_id: str
    task_branch: str
    task_commit_sha: str
    integration_merge_request_binding_id: str
    integration_merge_request_iid: int = Field(ge=1)
    integration_merge_commit_sha: str
    executor_type: str
    executor_id: str
    artifact_references: tuple[ArtifactReference, ...]
    external_validation: ExternalValidationReference

    @field_validator("task_commit_sha", "integration_merge_commit_sha")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("commit SHA must contain 40 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def validate_external_validation_binding(self) -> "IntegrationBaselineEvidenceItem":
        validation = self.external_validation
        if (
            validation.work_item_id != self.work_item_id
            or validation.integration_merge_request_binding_id
            != self.integration_merge_request_binding_id
            or validation.target_commit_sha != self.task_commit_sha
            or validation.integration_merge_commit_sha != self.integration_merge_commit_sha
            or validation.artifact_references != self.artifact_references
        ):
            raise ValueError("external validation does not bind the Evidence item exactly")
        if self.executor_type != "HUMAN" or not self.executor_id.strip():
            raise ValueError("V0.6 human Evidence requires an explicit human executor")
        return self


class EvidenceCurrentnessState(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class IntegrationBaselineEvidenceItemCurrentness(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    binding_id: str
    current_binding_id: str | None
    evidence_head_sha: str
    binding_head_sha: str | None
    latest_observation_head_sha: str | None
    evidence_merge_commit_sha: str
    latest_observation_merge_commit_sha: str | None
    external_validation_reference_id: str
    latest_external_validation_reference_id: str | None
    state: EvidenceCurrentnessState
    reasons: tuple[str, ...]

    @property
    def current(self) -> bool:
        return self.state is EvidenceCurrentnessState.CURRENT


class IntegrationBaselineEvidenceCurrentness(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: EvidenceCurrentnessState
    items: tuple[IntegrationBaselineEvidenceItemCurrentness, ...]

    @property
    def current(self) -> bool:
        return self.state is EvidenceCurrentnessState.CURRENT


def canonical_external_validation_hash(
    validation: ExternalValidationReference,
) -> str:
    return _canonical_hash(
        {
            "artifactReferences": [
                item.model_dump(mode="json")
                for item in sorted(
                    validation.artifact_references,
                    key=lambda item: (item.artifact_id, item.artifact_version),
                )
            ],
            "integrationMergeCommitSha": validation.integration_merge_commit_sha,
            "integrationMergeRequestBindingId": (validation.integration_merge_request_binding_id),
            "notes": validation.notes,
            "reference": validation.reference,
            "submittedBy": validation.submitted_by,
            "targetCommitSha": validation.target_commit_sha,
            "workItemId": validation.work_item_id,
        }
    )


def canonical_evidence_item_hash(item: IntegrationBaselineEvidenceItem) -> str:
    return _canonical_hash(item.model_dump(mode="json"))


def canonical_integration_baseline_hash(
    *,
    integration_baseline_id: str,
    requirement_id: str,
    requirement_version: int,
    required_work_item_set_version: int,
    required_work_item_set_hash: str,
    delivery_snapshot_id: str,
    delivery_snapshot_hash: str,
    items: Iterable[IntegrationBaselineEvidenceItem],
) -> str:
    normalized_items = tuple(sorted(items, key=lambda item: item.work_item_id))
    work_item_ids = tuple(item.work_item_id for item in normalized_items)
    if not work_item_ids:
        raise ValueError("Integration Baseline Evidence requires WorkItems")
    if len(set(work_item_ids)) != len(work_item_ids):
        raise ValueError("duplicate WorkItem Evidence is not allowed")
    if requirement_version < 1 or required_work_item_set_version < 1:
        raise ValueError("Evidence versions must be positive")
    if not _SHA256.fullmatch(required_work_item_set_hash) or not _SHA256.fullmatch(
        delivery_snapshot_hash
    ):
        raise ValueError("Evidence hashes must be canonical SHA-256")
    payload = {
        "deliverySnapshotHash": delivery_snapshot_hash,
        "deliverySnapshotId": delivery_snapshot_id,
        "integrationBaselineId": integration_baseline_id,
        "items": [item.model_dump(mode="json") for item in normalized_items],
        "requiredWorkItemSetHash": required_work_item_set_hash,
        "requiredWorkItemSetVersion": required_work_item_set_version,
        "requirementId": requirement_id,
        "requirementVersion": requirement_version,
    }
    return _canonical_hash(payload)


class IntegrationBaselineEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    delivery_snapshot_id: str
    delivery_snapshot_hash: str
    requirement_id: str
    requirement_version: int = Field(ge=1)
    required_work_item_set_version: int = Field(ge=1)
    required_work_item_set_hash: str
    evidence_hash: str
    items: tuple[IntegrationBaselineEvidenceItem, ...]
    currentness: IntegrationBaselineEvidenceCurrentness
    generated_by: str
    generated_at: datetime

    @field_validator(
        "delivery_snapshot_hash",
        "required_work_item_set_hash",
        "evidence_hash",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("Evidence hashes must be canonical SHA-256")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> "IntegrationBaselineEvidence":
        work_item_ids = tuple(item.work_item_id for item in self.items)
        if work_item_ids != tuple(sorted(work_item_ids)) or len(set(work_item_ids)) != len(
            work_item_ids
        ):
            raise ValueError("Evidence items must be unique and sorted by WorkItem")
        expected = canonical_integration_baseline_hash(
            integration_baseline_id=self.id,
            requirement_id=self.requirement_id,
            requirement_version=self.requirement_version,
            required_work_item_set_version=self.required_work_item_set_version,
            required_work_item_set_hash=self.required_work_item_set_hash,
            delivery_snapshot_id=self.delivery_snapshot_id,
            delivery_snapshot_hash=self.delivery_snapshot_hash,
            items=self.items,
        )
        if self.evidence_hash != expected:
            raise ValueError("Evidence hash does not match its immutable content")
        return self

    @property
    def work_item_ids(self) -> tuple[str, ...]:
        return tuple(item.work_item_id for item in self.items)
