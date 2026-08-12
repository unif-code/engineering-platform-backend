from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


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
