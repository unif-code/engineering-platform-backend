"""Public workspace facade; other modules must not import internals."""

from typing import Any

from sqlalchemy import Connection, Engine

from control_plane.app.modules.workspace.application import (
    WorkspaceDependencies,
    WorkspaceMembershipChangeHandler,
)
from control_plane.app.modules.workspace.application.commands import (
    create_workspace as _create_workspace,
)
from control_plane.app.modules.workspace.application.commands import invite_leader as _invite_leader
from control_plane.app.modules.workspace.application.commands import remove_leader as _remove_leader
from control_plane.app.modules.workspace.application.commands import (
    transfer_owner as _transfer_owner,
)
from control_plane.app.modules.workspace.application.projection import (
    recompute_members as _recompute_members,
)
from control_plane.app.modules.workspace.application.queries import (
    is_formal_member as _is_formal_member,
)
from control_plane.app.modules.workspace.application.queries import (
    list_workspaces as _list_workspaces,
)
from control_plane.app.modules.workspace.application.queries import members as _members
from control_plane.app.modules.workspace.domain import (
    FormalMemberDto,
    InvalidWorkspaceName,
    InvalidWorkspaceParticipant,
    LeaderAlreadyInvited,
    LeaderNotInvited,
    MemberSource,
    OwnerCannotBeRemoved,
    StaleWorkspaceVersion,
    WorkspaceArchived,
    WorkspaceDto,
    WorkspaceError,
    WorkspaceNotFound,
    WorkspaceOwnerRequired,
)


def create_workspace(
    db: Connection,
    *,
    name: str,
    owner_id: str,
    actor: Any,
    reason: str,
    dependencies: WorkspaceDependencies,
) -> WorkspaceDto:
    return _create_workspace(
        dependencies.repository_factory(db),
        name=name,
        owner_id=owner_id,
        actor=actor,
        reason=reason,
        dependencies=dependencies,
    )


def invite_leader(
    db: Connection,
    *,
    workspace_id: str,
    account_id: str,
    expected_version: int,
    actor: Any,
    reason: str,
    dependencies: WorkspaceDependencies,
) -> WorkspaceDto:
    return _invite_leader(
        dependencies.repository_factory(db),
        workspace_id=workspace_id,
        account_id=account_id,
        expected_version=expected_version,
        actor=actor,
        reason=reason,
        dependencies=dependencies,
    )


def remove_leader(
    db: Connection,
    *,
    workspace_id: str,
    account_id: str,
    expected_version: int,
    actor: Any,
    reason: str,
    dependencies: WorkspaceDependencies,
) -> WorkspaceDto:
    return _remove_leader(
        dependencies.repository_factory(db),
        workspace_id=workspace_id,
        account_id=account_id,
        expected_version=expected_version,
        actor=actor,
        reason=reason,
        dependencies=dependencies,
    )


def transfer_owner(
    db: Connection,
    *,
    workspace_id: str,
    new_owner_id: str,
    expected_version: int,
    actor: Any,
    reason: str,
    dependencies: WorkspaceDependencies,
) -> WorkspaceDto:
    return _transfer_owner(
        dependencies.repository_factory(db),
        workspace_id=workspace_id,
        new_owner_id=new_owner_id,
        expected_version=expected_version,
        actor=actor,
        reason=reason,
        dependencies=dependencies,
    )


def recompute_members(
    db: Connection,
    *,
    workspace_id: str,
    dependencies: WorkspaceDependencies,
) -> int:
    return _recompute_members(
        dependencies.repository_factory(db),
        workspace_id=workspace_id,
        dependencies=dependencies,
    )


def is_formal_member(
    db: Connection,
    *,
    workspace_id: str,
    account_id: str,
    dependencies: WorkspaceDependencies,
) -> bool:
    return _is_formal_member(
        dependencies.repository_factory(db),
        workspace_id=workspace_id,
        account_id=account_id,
        dependencies=dependencies,
    )


def list_workspaces(db: Connection, *, dependencies: WorkspaceDependencies) -> list[WorkspaceDto]:
    return _list_workspaces(dependencies.repository_factory(db))


def members(
    db: Connection,
    *,
    workspace_id: str,
    dependencies: WorkspaceDependencies,
) -> list[FormalMemberDto]:
    return _members(
        dependencies.repository_factory(db),
        workspace_id=workspace_id,
        dependencies=dependencies,
    )


def on_membership_change(
    engine: Engine,
    *,
    dependencies: WorkspaceDependencies,
) -> WorkspaceMembershipChangeHandler:
    return WorkspaceMembershipChangeHandler(engine, dependencies)


__all__ = [
    "FormalMemberDto",
    "InvalidWorkspaceName",
    "InvalidWorkspaceParticipant",
    "LeaderAlreadyInvited",
    "LeaderNotInvited",
    "MemberSource",
    "OwnerCannotBeRemoved",
    "StaleWorkspaceVersion",
    "WorkspaceArchived",
    "WorkspaceDependencies",
    "WorkspaceDto",
    "WorkspaceError",
    "WorkspaceMembershipChangeHandler",
    "WorkspaceNotFound",
    "WorkspaceOwnerRequired",
    "create_workspace",
    "invite_leader",
    "is_formal_member",
    "list_workspaces",
    "members",
    "on_membership_change",
    "recompute_members",
    "remove_leader",
    "transfer_owner",
]
