from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from control_plane.app.modules.configuration.domain.draft_directory import DraftListItem
from control_plane.app.modules.configuration.domain.models import PolicySnapshot


class DraftArchiveTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    draft: DraftListItem
    current: PolicySnapshot
    archive_after_days: int = Field(gt=0)
    observed_at: AwareDatetime
    expected_archive_at: AwareDatetime | None
    inactivity_elapsed: bool | None
