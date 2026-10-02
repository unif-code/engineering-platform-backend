"""Immutable declared references and exact terminal evidence, never activation facts."""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from control_plane.app.modules.model_gateway.domain import _non_secret
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckReason,
    CheckState,
    ConnectionCheck,
    CurrentnessReason,
    InputCurrentness,
    ProbeUsage,
)
from control_plane.app.modules.model_gateway.domain.connections import CheckKind, digest
from control_plane.app.shared.security import sanitize_external_reference

MAX_DOSSIER_BODY_BYTES = 65536


def _text(value: object) -> object:
    if not isinstance(value, str):
        return value
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("control characters are not material metadata")
    return _non_secret(value)


def _reference(value: str) -> str:
    _text(unquote(value))
    if any(char.isspace() for char in value) or "\\" in value:
        raise ValueError("invalid material reference")
    parts = urlsplit(value)
    if parts.scheme == "https":
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError("invalid HTTPS reference")
        _ = parts.port
    elif (
        parts.scheme != "urn"
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}:.+", parts.path) is None
    ):
        raise ValueError("only HTTPS or stable URN references are accepted")
    return sanitize_external_reference(value)


def _datetime_input(value: object) -> object:
    if not isinstance(value, (str, datetime)):
        raise ValueError("an ISO 8601 timestamp with timezone is required")
    return value


def _utc_time(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise ValueError("timestamp is outside the supported UTC range") from None


DossierTime = Annotated[
    AwareDatetime,
    BeforeValidator(_datetime_input),
    AfterValidator(_utc_time),
]
DossierId = Annotated[
    str,
    StringConstraints(strict=True, min_length=36, max_length=36),
    AfterValidator(lambda value: str(UUID(value))),
    Field(json_schema_extra={"format": "uuid"}),
]
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
MaterialTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
    BeforeValidator(_text),
]
MaterialNote = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    BeforeValidator(_text),
]
ExternalVersion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    BeforeValidator(_text),
]
SourceReference = Annotated[
    str, StringConstraints(min_length=5, max_length=2048), AfterValidator(_reference)
]
TerminalState = Literal[
    CheckState.SUCCEEDED, CheckState.FAILED, CheckState.BLOCKED, CheckState.UNKNOWN
]


class MaterialCategory(StrEnum):
    MODEL_IDENTITY = "MODEL_IDENTITY"
    CAPABILITY_SPEC = "CAPABILITY_SPEC"
    CONTEXT_LIMITS = "CONTEXT_LIMITS"
    PARAMETER_SCHEMA = "PARAMETER_SCHEMA"
    PRICING = "PRICING"
    QUOTA = "QUOTA"
    DATA_PROCESSING = "DATA_PROCESSING"
    HEALTH = "HEALTH"


class MaterialReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    category: MaterialCategory
    title: MaterialTitle
    source_reference: SourceReference
    external_version: ExternalVersion | None = None
    declared_content_sha256: Sha256 | None = None
    expires_at: DossierTime | None = None
    note: MaterialNote | None = None


class DeclaredMaterial(MaterialReference):
    provenance: Literal["DECLARED"] = "DECLARED"


class CreateValidationDossier(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_extra={"x-maxBodyBytes": MAX_DOSSIER_BODY_BYTES}
    )
    materials: tuple[MaterialReference, ...] = Field(default=(), max_length=32)
    check_ids: tuple[DossierId, ...] = Field(
        default=(), max_length=5, json_schema_extra={"uniqueItems": True}
    )

    @model_validator(mode="after")
    def has_explicit_material(self) -> Self:
        if not self.materials and not self.check_ids:
            raise ValueError("at least one material or terminal check is required")
        if len(set(self.check_ids)) != len(self.check_ids):
            raise ValueError("check IDs must be distinct")
        return self


class CheckResultSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: TerminalState
    reason: CheckReason | None
    finished_at: DossierTime | None
    reported_model_id: str | None
    provider_request_id: str | None
    elapsed_ms: int | None
    usage: ProbeUsage | None


class CheckEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    check_id: DossierId
    check_revision: int = Field(ge=1)
    check_kind: CheckKind
    input_digest: Sha256
    result_summary: CheckResultSummary
    result_hash: Sha256

    @model_validator(mode="after")
    def hash_matches_summary(self) -> Self:
        if self.result_hash != digest(self.result_summary.model_dump(mode="json")):
            raise ValueError("check result hash mismatch")
        return self

    @classmethod
    def capture(cls, check: ConnectionCheck) -> "CheckEvidenceReference":
        summary = CheckResultSummary.model_validate(
            check.model_dump(include=set(CheckResultSummary.model_fields))
        )
        return cls(
            check_id=check.id,
            check_revision=check.revision,
            check_kind=check.check_kind,
            input_digest=check.input.input_digest,
            result_summary=summary,
            result_hash=digest(summary.model_dump(mode="json")),
        )


class ValidationDossier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: DossierId
    deployment_id: DossierId
    candidate_revision: int = Field(ge=1)
    created_by: str = Field(min_length=1)
    created_at: DossierTime
    materials: tuple[DeclaredMaterial, ...] = Field(max_length=32)
    checks: tuple[CheckEvidenceReference, ...] = Field(max_length=5)
    snapshot_hash: Sha256

    @model_validator(mode="after")
    def snapshot_integrity(self) -> Self:
        if not self.materials and not self.checks:
            raise ValueError("empty dossier")
        if len({check.check_kind for check in self.checks}) != len(self.checks):
            raise ValueError("only one terminal check per kind is accepted")
        if self.snapshot_hash != digest(self.model_dump(mode="json", exclude={"snapshot_hash"})):
            raise ValueError("dossier snapshot hash mismatch")
        return self


class MaterialExpiration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    material_index: int = Field(ge=0, le=31)
    expiration: Literal["NOT_DECLARED", "NOT_EXPIRED", "EXPIRED"]


class MaterialCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    category: MaterialCategory
    status: Literal["MISSING", "DECLARED", "EXPIRED"]
    registered_count: int = Field(ge=0, le=32)
    expired_count: int = Field(ge=0, le=32)


class DossierCheckCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    check_kind: CheckKind
    status: Literal["NOT_PROVIDED", "PROVIDED"]
    check_id: DossierId | None
    currentness: InputCurrentness | None
    currentness_reasons: tuple[
        CurrentnessReason | Literal["CHECK_UNAVAILABLE", "CHECK_SNAPSHOT_MISMATCH"], ...
    ]


class ValidationDossierProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: ValidationDossier
    currentness: InputCurrentness
    currentness_reasons: tuple[
        Literal["CANDIDATE_CHANGED", "CANDIDATE_ARCHIVED", "CHECK_STALE", "CHECK_UNVERIFIABLE"], ...
    ]
    material_statuses: tuple[MaterialExpiration, ...]
    material_coverage: tuple[MaterialCoverage, ...]
    check_coverage: tuple[DossierCheckCoverage, ...]
