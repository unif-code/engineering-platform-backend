from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from control_plane.app.modules.configuration.domain.draft_directory import DraftDirectoryId


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class StoredArchiveEvent(_SourceModel):
    id: DraftDirectoryId
    namespace: str = Field(min_length=1)
    scope: Literal["PLATFORM"]
    event_type: Literal["DRAFT_ARCHIVED"]
    aggregate_id: str = Field(min_length=1)
    payload: dict[str, Any]
    occurred_at: AwareDatetime


class ManualArchiveRecord(_SourceModel):
    source: Literal["MANUAL"]
    id: DraftDirectoryId
    recorded_at: AwareDatetime
    actor_id: str = Field(min_length=1)
    actor_type: Literal["human"]
    draft_revision: int = Field(gt=0)


class InactivityArchiveRecord(_SourceModel):
    source: Literal["INACTIVITY"]
    id: DraftDirectoryId
    recorded_at: AwareDatetime
    policy_version: int = Field(gt=0)
    cutoff: AwareDatetime


class DraftArchiveSource(_SourceModel):
    draft_id: DraftDirectoryId
    namespace: str = Field(min_length=1)
    scope: Literal["PLATFORM"]
    draft_revision: int = Field(gt=0)
    status: Literal["DRAFT", "ARCHIVED"]
    archived_at: AwareDatetime | None
    record_status: Literal["NOT_ARCHIVED", "RECORDED", "UNKNOWN"]
    record: (
        Annotated[ManualArchiveRecord | InactivityArchiveRecord, Field(discriminator="source")]
        | None
    )
