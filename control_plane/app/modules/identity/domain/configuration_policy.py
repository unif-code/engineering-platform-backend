from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class OwnedPolicySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    namespace: str
    scope: str
    version: int
    schema_revision: int
    snapshot_hash: str
    values: dict[str, Any]


class OwnedPolicyKey(BaseModel):
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


class OwnedPolicyDraft(BaseModel):
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
    validation_content_hash: str | None
    validation_schema_revision: int | None
    validation_base_version: int | None
    validation_dependency_versions: dict[str, Any] | None


class OwnedPolicySnapshotUnavailable(RuntimeError):
    """The identity-owned active policy cannot be read as a complete snapshot."""
