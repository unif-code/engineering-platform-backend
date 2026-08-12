from datetime import datetime
from typing import Any

from pydantic import Field

from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftValidation,
    PolicyKey,
    PolicySnapshot,
    Preview,
    PreviewItem,
    PublishedVersion,
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


class PublishDraftRequestDto(CamelModel):
    reason: str = Field(min_length=1, max_length=1000)
    totp_code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


class RollbackPolicyRequestDto(CamelModel):
    scope: str = Field(default="PLATFORM", min_length=1)
    to_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    totp_code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


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
    rollback_from_version: int | None = None
    preview_evidence: dict[str, Any] | None = None

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


class PreviewItemDto(CamelModel):
    key: str
    before: Any
    after: Any
    effect_semantics: str
    impact: str

    @classmethod
    def from_domain(cls, value: PreviewItem) -> "PreviewItemDto":
        return cls.model_validate(value.model_dump())


class PreviewResponseDto(CamelModel):
    draft_id: str
    revision: int
    content_hash: str
    base_version: int
    items: list[PreviewItemDto]

    @classmethod
    def from_domain(cls, value: Preview) -> "PreviewResponseDto":
        return cls(
            draft_id=value.draft_id,
            revision=value.revision,
            content_hash=value.content_hash,
            base_version=value.base_version,
            items=[PreviewItemDto.from_domain(item) for item in value.items],
        )


class PublishedVersionDto(CamelModel):
    namespace: str
    scope: str
    version: int
    snapshot: dict[str, Any]
    snapshot_hash: str
    published_by: str
    reason: str
    published_at: datetime
    activated_at: datetime
    schema_revision: int

    @classmethod
    def from_domain(cls, value: PublishedVersion) -> "PublishedVersionDto":
        return cls.model_validate(value.model_dump())


class PolicyVersionsResponseDto(CamelModel):
    items: list[PublishedVersionDto]
    next_cursor: str | None
