from control_plane.app.modules.workspace.application.common import is_effective, workspace_dto
from control_plane.app.modules.workspace.application.dependencies import WorkspaceDependencies
from control_plane.app.modules.workspace.domain import (
    FormalMemberDto,
    MemberSource,
    WorkspaceArchived,
    WorkspaceDto,
    WorkspaceNotFound,
)
from control_plane.app.modules.workspace.ports import WorkspaceRepository


def list_workspaces(repository: WorkspaceRepository) -> list[WorkspaceDto]:
    return [workspace_dto(row) for row in repository.list_workspaces()]


def members(
    repository: WorkspaceRepository,
    *,
    workspace_id: str,
    dependencies: WorkspaceDependencies,
) -> list[FormalMemberDto]:
    workspace = repository.workspace_by_id(workspace_id)
    if workspace is None:
        raise WorkspaceNotFound(workspace_id)
    if workspace["archived_at"] is not None:
        raise WorkspaceArchived(workspace_id)
    return [
        FormalMemberDto(
            account_id=str(row["account_id"]),
            source=MemberSource(row["source"]),
            computed_at=row["computed_at"],
        )
        for row in repository.projection_rows(workspace_id)
        if is_effective(dependencies.identity.get(str(row["account_id"])))
    ]


def is_formal_member(
    repository: WorkspaceRepository,
    *,
    workspace_id: str,
    account_id: str,
    dependencies: WorkspaceDependencies,
) -> bool:
    workspace = repository.workspace_by_id(workspace_id)
    if workspace is None or workspace["archived_at"] is not None:
        return False
    if not is_effective(dependencies.identity.get(account_id)):
        return False
    return any(
        str(row["account_id"]) == account_id for row in repository.projection_rows(workspace_id)
    )
