from datetime import datetime

from pydantic import Field

from control_plane.app.modules.workspace.domain import FormalMemberDto, WorkspaceDto
from control_plane.app.shared.api.camel import CamelModel


class CreateWorkspaceRequestDto(CamelModel):
    name: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class InviteLeaderRequestDto(CamelModel):
    account_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class RemoveLeaderRequestDto(CamelModel):
    reason: str = Field(min_length=1, max_length=500)


class TransferOwnerRequestDto(CamelModel):
    new_owner_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class WorkspaceResponseDto(CamelModel):
    id: str
    name: str
    owner_id: str
    archived_at: datetime | None
    version: int

    @classmethod
    def from_domain(cls, workspace: WorkspaceDto) -> "WorkspaceResponseDto":
        return cls(
            id=workspace.id,
            name=workspace.name,
            owner_id=workspace.owner_id,
            archived_at=workspace.archived_at,
            version=workspace.version,
        )


class WorkspaceListResponseDto(CamelModel):
    items: list[WorkspaceResponseDto]
    next_cursor: str | None = None


class FormalMemberResponseDto(CamelModel):
    account_id: str
    source: str
    computed_at: datetime

    @classmethod
    def from_domain(cls, member: FormalMemberDto) -> "FormalMemberResponseDto":
        return cls(
            account_id=member.account_id,
            source=member.source.value,
            computed_at=member.computed_at,
        )


class FormalMemberListResponseDto(CamelModel):
    items: list[FormalMemberResponseDto]
    next_cursor: str | None = None
