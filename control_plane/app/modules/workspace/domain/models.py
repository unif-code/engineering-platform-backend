from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class WorkspaceError(ValueError):
    """A deterministic workspace governance denial."""


class WorkspaceNotFound(WorkspaceError):
    pass


class WorkspaceArchived(WorkspaceError):
    pass


class InvalidWorkspaceParticipant(WorkspaceError):
    pass


class InvalidWorkspaceName(WorkspaceError):
    pass


class WorkspaceOwnerRequired(WorkspaceError):
    pass


class StaleWorkspaceVersion(WorkspaceError):
    pass


class LeaderAlreadyInvited(WorkspaceError):
    pass


class LeaderNotInvited(WorkspaceError):
    pass


class OwnerCannotBeRemoved(WorkspaceError):
    pass


class MemberSource(StrEnum):
    OWNER = "OWNER"
    LEADER = "LEADER"
    DIRECT_REPORT = "DIRECT_REPORT"


class WorkspaceDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    owner_id: str
    archived_at: datetime | None
    version: int


class FormalMemberDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    source: MemberSource
    computed_at: datetime
