from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from control_plane.app.modules.configuration.domain.models import (
    DraftBaseChange,
    DraftCloneSource,
    PolicySnapshot,
)

Identifier = Annotated[str, Field(min_length=1)]
Positive = Annotated[int, Field(gt=0)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: Identifier
    draft_id: Identifier
    namespace: Identifier
    scope: Literal["PLATFORM"]
    schema_revision: Positive
    actor_id: Identifier
    recorded_at: AwareDatetime


class _StoredRecord(_Record):
    base_version: Positive
    current_version: Positive
    base_snapshot_hash: Digest
    current_snapshot_hash: Digest


class StoredCloneRecord(_StoredRecord):
    source_draft_id: Identifier
    source_revision: Positive
    source_owner_id: Identifier
    source_status: Literal["DRAFT", "ARCHIVED"]
    source_content: dict[str, Any]
    source_content_hash: Digest
    content_hash: Digest
    rollback_from_version: Positive | None
    cloned_from_archived_draft_id: Identifier | None


class StoredRebaseRecord(_StoredRecord):
    before_revision: Positive
    after_revision: Positive
    before_content: dict[str, Any]
    after_content: dict[str, Any]
    before_content_hash: Digest
    after_content_hash: Digest
    selections: dict[str, dict[str, Any]]


class DraftCloneRecord(_Record):
    source: DraftCloneSource
    base_snapshot: PolicySnapshot
    current_snapshot_at_operation: PolicySnapshot
    source_content: dict[str, Any]
    created_content_hash: Digest


class DraftRebaseSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    change: DraftBaseChange
    source: Literal["BASE", "CURRENT", "DRAFT", "CUSTOM"]
    resolution: dict[str, Any] | None


class DraftRebaseRecord(_Record):
    before_revision: Positive
    after_revision: Positive
    base_snapshot: PolicySnapshot
    current_snapshot_at_operation: PolicySnapshot
    before_content: dict[str, Any]
    after_content: dict[str, Any]
    before_content_hash: Digest
    after_content_hash: Digest
    selections: dict[str, DraftRebaseSelection]


class DraftGovernanceRecords(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    draft_id: Identifier
    namespace: Identifier
    scope: Literal["PLATFORM"]
    draft_revision: Positive
    clone_record: DraftCloneRecord | None
    rebases: list[DraftRebaseRecord]
    next_cursor: str | None
