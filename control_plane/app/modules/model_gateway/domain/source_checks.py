"""Evidence about approved local copies, never source authenticity or Model activation."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.app.modules.model_gateway.domain.checks import InputCurrentness
from control_plane.app.modules.model_gateway.domain.connections import VersionLabel, digest
from control_plane.app.modules.model_gateway.domain.dossiers import (
    DossierId,
    DossierTime,
    ExternalVersion,
    Sha256,
    SourceReference,
)

MAX_SOURCE_BYTES = 65536
MAX_SOURCE_ENTRIES = 100


class SourceCheckResult(StrEnum):
    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"
    BLOCKED = "BLOCKED"


class SourceCheckReason(StrEnum):
    DECLARED_HASH_MISSING = "DECLARED_HASH_MISSING"
    DECLARED_HASH_MISMATCH = "DECLARED_HASH_MISMATCH"
    SOURCE_DIRECTORY_UNCONFIGURED = "SOURCE_DIRECTORY_UNCONFIGURED"
    SOURCE_DIRECTORY_INVALID = "SOURCE_DIRECTORY_INVALID"
    SOURCE_ENVIRONMENT_MISMATCH = "SOURCE_ENVIRONMENT_MISMATCH"
    SOURCE_NOT_APPROVED = "SOURCE_NOT_APPROVED"
    SOURCE_COPY_UNAVAILABLE = "SOURCE_COPY_UNAVAILABLE"
    SOURCE_COPY_NOT_REGULAR = "SOURCE_COPY_NOT_REGULAR"
    SOURCE_COPY_TOO_LARGE = "SOURCE_COPY_TOO_LARGE"
    SOURCE_COPY_CHANGED = "SOURCE_COPY_CHANGED"
    SOURCE_COPY_EMPTY = "SOURCE_COPY_EMPTY"
    APPROVED_COPY_HASH_MISMATCH = "APPROVED_COPY_HASH_MISMATCH"


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: VersionLabel | None = None
    source_version: VersionLabel | None = None
    entry_fingerprint: Sha256 | None = None
    approved_copy_sha256: Sha256 | None = None
    observed_sha256: Sha256 | None = None
    observed_bytes: int | None = Field(default=None, strict=True, ge=1, le=MAX_SOURCE_BYTES)

    @model_validator(mode="after")
    def complete_groups(self) -> Self:
        identity = (
            self.source_id,
            self.source_version,
            self.entry_fingerprint,
            self.approved_copy_sha256,
        )
        if any(value is None for value in identity) and any(
            value is not None for value in identity
        ):
            raise ValueError("source identity must be complete or unavailable")
        if (self.observed_sha256 is None) != (self.observed_bytes is None):
            raise ValueError("complete measurements require both digest and byte count")
        if self.observed_sha256 is not None and self.source_id is None:
            raise ValueError("a measurement requires an approved mapping identity")
        return self


class SourceInspection(SourceEvidence):
    reason: SourceCheckReason | None

    @model_validator(mode="after")
    def valid_measurement(self) -> Self:
        if self.reason in (
            SourceCheckReason.DECLARED_HASH_MISSING,
            SourceCheckReason.DECLARED_HASH_MISMATCH,
        ):
            raise ValueError("source Port does not judge caller declarations")
        if self.reason is None:
            if self.observed_sha256 is None or self.observed_sha256 != self.approved_copy_sha256:
                raise ValueError("approved complete measurement required")
        elif self.reason == SourceCheckReason.APPROVED_COPY_HASH_MISMATCH:
            if self.observed_sha256 is None or self.observed_sha256 == self.approved_copy_sha256:
                raise ValueError("complete mismatching copy measurement required")
        elif self.observed_sha256 is not None:
            raise ValueError("incomplete/unavailable reads cannot claim a measurement")
        return self


def compare_declared_source(
    declared: str | None, observed: SourceInspection | None
) -> tuple[SourceCheckResult, SourceCheckReason | None]:
    if declared is None:
        return SourceCheckResult.BLOCKED, SourceCheckReason.DECLARED_HASH_MISSING
    if observed is None:
        raise ValueError("declared material requires an inspection result")
    if observed.reason is not None:
        return SourceCheckResult.BLOCKED, observed.reason
    if observed.observed_sha256 == declared:
        return SourceCheckResult.MATCHED, None
    return SourceCheckResult.MISMATCH, SourceCheckReason.DECLARED_HASH_MISMATCH


class MaterialSourceCheck(SourceEvidence):
    id: DossierId
    deployment_id: DossierId
    candidate_revision: int = Field(ge=1)
    dossier_id: DossierId
    dossier_snapshot_hash: Sha256
    material_index: int = Field(strict=True, ge=0, le=31)
    source_reference: SourceReference
    external_version: ExternalVersion | None
    declared_content_sha256: Sha256 | None
    result: SourceCheckResult
    reason: SourceCheckReason | None
    created_by: str = Field(min_length=1)
    created_at: DossierTime
    snapshot_hash: Sha256

    @model_validator(mode="after")
    def immutable_result(self) -> Self:
        observed = None
        if self.declared_content_sha256 is not None:
            observed = SourceInspection.model_validate(
                self.model_dump(include=set(SourceInspection.model_fields))
                | {
                    "reason": None if self.result == SourceCheckResult.MISMATCH else self.reason,
                }
            )
        elif any(getattr(self, field) is not None for field in SourceEvidence.model_fields):
            raise ValueError("missing declarations must not inspect sources")
        if (self.result, self.reason) != compare_declared_source(
            self.declared_content_sha256, observed
        ):
            raise ValueError("source result does not match measured evidence")
        if self.snapshot_hash != digest(self.model_dump(mode="json", exclude={"snapshot_hash"})):
            raise ValueError("source check snapshot hash mismatch")
        return self


class SourceCheckCurrentnessReason(StrEnum):
    CANDIDATE_CHANGED = "CANDIDATE_CHANGED"
    CANDIDATE_ARCHIVED = "CANDIDATE_ARCHIVED"
    DOSSIER_BINDING_CHANGED = "DOSSIER_BINDING_CHANGED"
    SOURCE_BINDING_CHANGED = "SOURCE_BINDING_CHANGED"
    SOURCE_NO_LONGER_APPROVED = "SOURCE_NO_LONGER_APPROVED"
    SOURCE_CONTENT_CHANGED = "SOURCE_CONTENT_CHANGED"
    SOURCE_UNVERIFIABLE = "SOURCE_UNVERIFIABLE"
    ORIGINAL_MEASUREMENT_UNAVAILABLE = "ORIGINAL_MEASUREMENT_UNAVAILABLE"


class MaterialSourceCheckProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: MaterialSourceCheck
    currentness: InputCurrentness
    currentness_reasons: tuple[SourceCheckCurrentnessReason, ...]
    material_expires_at: DossierTime | None
    material_expiration: Literal["NOT_DECLARED", "NOT_EXPIRED", "EXPIRED"]
