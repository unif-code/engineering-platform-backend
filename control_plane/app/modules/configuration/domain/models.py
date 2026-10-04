from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

DraftBaseChange = Literal["UNCHANGED", "CURRENT_ONLY", "DRAFT_ONLY", "SAME_CHANGE", "CONFLICT"]


class PolicySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    namespace: str
    scope: str
    version: int
    schema_revision: int
    snapshot_hash: str
    values: dict[str, Any]


class PolicyKey(BaseModel):
    model_config = ConfigDict(frozen=True)

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


class Draft(BaseModel):
    model_config = ConfigDict(frozen=True)

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


class DraftCloneSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    revision: int
    owner_id: str
    status: Literal["DRAFT", "ARCHIVED"]
    base_version: int
    schema_revision: int
    content_hash: str
    rollback_from_version: int | None
    cloned_from_archived_draft_id: str | None


class DraftClone(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: DraftCloneSource
    current_version_at_clone: int
    draft: Draft


class DraftBaseComparisonItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    value_type: str
    unit: str | None
    base_value: Any
    current_value: Any
    draft_value: Any
    change: DraftBaseChange


class DraftBaseComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    namespace: str
    scope: Literal["PLATFORM"]
    owner_id: str
    draft_revision: int
    status: Literal["DRAFT", "ARCHIVED"]
    schema_revision: int
    base_version: int
    current_version: int
    base_snapshot_hash: str
    current_snapshot_hash: str
    draft_content_hash: str
    items: list[DraftBaseComparisonItem]


class PreviewItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    before: Any
    after: Any
    effect_semantics: str
    impact: str


class Preview(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    revision: int
    content_hash: str
    base_version: int
    items: list[PreviewItem]


class PublishedVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

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


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    key: str
    message: str


class DraftValidation(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    revision: int
    content_hash: str
    valid: bool
    issues: list[ValidationIssue]


class PolicySnapshotUnavailable(RuntimeError):
    """The active snapshot is absent, inconsistent, or unreadable."""


class ConfigurationError(RuntimeError):
    """Base class for safe configuration lifecycle conflicts."""


class DraftAuthorizationDenied(ConfigurationError):
    def __init__(self, status_code: Literal[401, 403]) -> None:
        super().__init__("Current draft authorization denied")
        self.status_code = status_code


class DraftNotFound(ConfigurationError):
    pass


class DraftOwnerRequired(ConfigurationError):
    pass


class StaleDraftRevision(ConfigurationError):
    pass


class StaleDraftBase(ConfigurationError):
    pass


class DraftArchived(ConfigurationError):
    pass


class InvalidPolicyValue(ConfigurationError):
    pass


class SourceStale(ConfigurationError):
    pass


class PolicyVerificationFailed(ConfigurationError):
    pass


class PolicyVersionNotFound(ConfigurationError):
    pass
