from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field

from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftBaseChange,
    DraftBaseComparison,
    DraftClone,
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
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any] = Field(default_factory=dict)


class ValidateDraftRequestDto(CamelModel):
    pass


class PublishDraftRequestDto(CamelModel):
    reason: str = Field(min_length=1, max_length=1000)
    totp_code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


class TakeoverDraftRequestDto(CamelModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


class RebaseSideResolutionDto(CamelModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    choice: Literal["CURRENT", "DRAFT"]


class RebaseCustomResolutionDto(CamelModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    choice: Literal["CUSTOM"]
    value: Any


class ApplyDraftRebaseRequestDto(CamelModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_version: int = Field(gt=0)
    current_version: int = Field(gt=0)
    schema_revision: int = Field(gt=0)
    base_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolutions: dict[
        str,
        Annotated[
            RebaseSideResolutionDto | RebaseCustomResolutionDto, Field(discriminator="choice")
        ],
    ]


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


class CloneDraftRequestDto(CamelModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DraftCloneSourceDto(CamelModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    draft_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    owner_id: str = Field(min_length=1)
    status: Literal["DRAFT", "ARCHIVED"]
    base_version: int = Field(ge=1)
    schema_revision: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_from_version: int | None = Field(ge=1)
    cloned_from_archived_draft_id: str | None = Field(min_length=1)


class DraftCloneResponseDto(CamelModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: DraftCloneSourceDto
    current_version_at_clone: int = Field(ge=1)
    draft: DraftResponseDto

    @classmethod
    def from_domain(cls, value: DraftClone) -> "DraftCloneResponseDto":
        return cls.model_validate(value.model_dump())


class DraftBaseComparisonItemDto(CamelModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str = Field(min_length=1)
    value_type: str = Field(min_length=1)
    unit: str | None
    base_value: Any
    current_value: Any
    draft_value: Any
    change: DraftBaseChange


class DraftBaseComparisonResponseDto(CamelModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    draft_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    scope: Literal["PLATFORM"]
    owner_id: str = Field(min_length=1)
    draft_revision: int = Field(ge=1)
    status: Literal["DRAFT", "ARCHIVED"]
    schema_revision: int = Field(ge=1)
    base_version: int = Field(ge=1)
    current_version: int = Field(ge=1)
    base_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[DraftBaseComparisonItemDto]

    @classmethod
    def from_domain(cls, value: DraftBaseComparison) -> "DraftBaseComparisonResponseDto":
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
