from datetime import datetime
from typing import Any

from pydantic import Field

from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftValidation,
    PolicyKey,
    PolicySnapshot,
    ValidationIssue,
)
from control_plane.app.shared.api.camel import CamelModel


class PolicyKeyDto(CamelModel):
    key: str
    namespace: str
    value_type: str
    unit: str | None
    default_value: Any
    min_value: Any | None
    max_value: Any | None
    enum_values: list[Any] | None
    effect_semantics: str
    schema_revision: int

    @classmethod
    def from_domain(cls, value: PolicyKey) -> "PolicyKeyDto":
        return cls.model_validate(value.model_dump())


class PolicySnapshotDto(CamelModel):
    namespace: str
    scope: str
    version: int
    schema_revision: int
    snapshot_hash: str
    values: dict[str, Any]

    @classmethod
    def from_domain(cls, value: PolicySnapshot) -> "PolicySnapshotDto":
        return cls.model_validate(value.model_dump())


class PolicyCatalogResponseDto(CamelModel):
    items: list[PolicyKeyDto]
    active: PolicySnapshotDto


class DraftValuesRequestDto(CamelModel):
    values: dict[str, Any] = Field(default_factory=dict)


class ValidateDraftRequestDto(CamelModel):
    pass


class DraftResponseDto(CamelModel):
    id: str
    namespace: str
    scope: str
    content: dict[str, Any]
    base_version: int
    owner_id: str
    revision: int
    status: str
    stale: bool
    last_meaningful_activity_at: datetime
    archived_at: datetime | None
    schema_revision: int
    content_hash: str
    validation_evidence: dict[str, Any] | None

    @classmethod
    def from_domain(cls, value: Draft) -> "DraftResponseDto":
        return cls.model_validate(value.model_dump())


class ValidationIssueDto(CamelModel):
    code: str
    key: str
    message: str

    @classmethod
    def from_domain(cls, value: ValidationIssue) -> "ValidationIssueDto":
        return cls.model_validate(value.model_dump())


class DraftValidationResponseDto(CamelModel):
    draft_id: str
    revision: int
    content_hash: str
    valid: bool
    issues: list[ValidationIssueDto]

    @classmethod
    def from_domain(cls, value: DraftValidation) -> "DraftValidationResponseDto":
        return cls(
            draft_id=value.draft_id,
            revision=value.revision,
            content_hash=value.content_hash,
            valid=value.valid,
            issues=[ValidationIssueDto.from_domain(issue) for issue in value.issues],
        )
