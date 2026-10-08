from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

DraftDirectoryView = Literal["ALL", "ACTIVE", "STALE", "ARCHIVED"]
DraftDirectoryOwner = Literal["ALL", "MINE"]
DRAFT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
DraftDirectoryId = Annotated[str, Field(pattern=DRAFT_ID_PATTERN)]


class DraftSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: DraftDirectoryId
    namespace: str = Field(min_length=1)
    scope: Literal["PLATFORM"]
    owner_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    status: Literal["DRAFT", "ARCHIVED"]
    base_version: int = Field(gt=0)
    schema_revision: int = Field(gt=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    last_meaningful_activity_at: AwareDatetime
    archived_at: AwareDatetime | None
    rollback_from_version: Annotated[int, Field(gt=0)] | None


class DraftListItem(DraftSummary):
    base_behind: bool


class DraftList(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    namespace: str = Field(min_length=1)
    scope: Literal["PLATFORM"]
    view: DraftDirectoryView
    owner: DraftDirectoryOwner
    current_version: int = Field(gt=0)
    items: list[DraftListItem]
    next_cursor: DraftDirectoryId | None
