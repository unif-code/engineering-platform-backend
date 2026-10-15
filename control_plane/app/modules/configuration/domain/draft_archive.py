from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class DraftArchive(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    draft_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    scope: Literal["PLATFORM"]
    draft_revision: int = Field(gt=0)
    status: Literal["ARCHIVED"]
    archived_at: AwareDatetime
