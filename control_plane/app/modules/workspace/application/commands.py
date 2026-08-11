from typing import Any

from control_plane.app.modules.workspace.application.common import (
    actor_id,
    audit,
    is_effective,
    workspace_dto,
)
from control_plane.app.modules.workspace.application.dependencies import WorkspaceDependencies
from control_plane.app.modules.workspace.application.projection import _replace_projection
from control_plane.app.modules.workspace.domain import (
    InvalidWorkspaceName,
    InvalidWorkspaceParticipant,
    LeaderAlreadyInvited,
    LeaderNotInvited,
    OwnerCannotBeRemoved,
    StaleWorkspaceVersion,
    WorkspaceArchived,
    WorkspaceDto,
    WorkspaceNotFound,
    WorkspaceOwnerRequired,
)
from control_plane.app.modules.workspace.ports import WorkspaceRepository


def _valid_leader(account_id: str, dependencies: WorkspaceDependencies) -> bool:
    return is_effective(dependencies.identity.get(account_id)) and (
        dependencies.organization.is_effective_leader(account_id)
    )


def _locked_owned_workspace(
    repository: WorkspaceRepository,
    *,
    workspace_id: str,
    expected_version: int,
    actor: Any,
) -> Any:
    row = repository.workspace_by_id(workspace_id, for_update=True)
    if row is None:
        raise WorkspaceNotFound(workspace_id)
    if row["archived_at"] is not None:
        raise WorkspaceArchived(workspace_id)
    if row["owner_id"] != actor_id(actor):
        raise WorkspaceOwnerRequired(workspace_id)
    if row["version"] != expected_version:
        raise StaleWorkspaceVersion(workspace_id)
    return row


def _audit_projection(
    repository: WorkspaceRepository,
    *,
    dependencies: WorkspaceDependencies,
    workspace_id: str,
    count: int,
    version: int,
) -> None:
    audit(
        repository,
        dependencies=dependencies,
        actor="SYSTEM",
        action="workspace.members.recomputed",
        workspace_id=workspace_id,
        reason=f"count={count}; changed=true; version={version}",
    )


def create_workspace(
    repository: WorkspaceRepository,
    *,
    name: str,
    owner_id: str,
    actor: Any,
    reason: str,
    dependencies: WorkspaceDependencies,
) -> WorkspaceDto:
    normalized_name = name.strip()
    if not normalized_name:
        raise InvalidWorkspaceName("workspace name is required")
    if actor_id(actor) != owner_id or not _valid_leader(owner_id, dependencies):
        raise InvalidWorkspaceParticipant("workspace owner must be the creating leader")
    workspace_id = str(dependencies.random.uuid4())
    row = repository.insert_workspace(
        workspace_id=workspace_id,
        name=normalized_name,
        owner_id=owner_id,
    )
    count, _changed = _replace_projection(
        repository,
        workspace_id=workspace_id,
        owner_id=owner_id,
        dependencies=dependencies,
    )
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_id(actor),
        action="workspace.created",
        workspace_id=workspace_id,
        reason=f"{reason}; version=1",
    )
    _audit_projection(
        repository,
        dependencies=dependencies,
        workspace_id=workspace_id,
        count=count,
        version=1,
    )
    return workspace_dto(row)


def invite_leader(
    repository: WorkspaceRepository,
    *,
    workspace_id: str,
    account_id: str,
    expected_version: int,
    actor: Any,
    reason: str,
    dependencies: WorkspaceDependencies,
) -> WorkspaceDto:
    row = _locked_owned_workspace(
        repository,
        workspace_id=workspace_id,
        expected_version=expected_version,
        actor=actor,
    )
    if not _valid_leader(account_id, dependencies):
        raise InvalidWorkspaceParticipant("invited account must be an effective leader")
    if not repository.insert_leader(
        workspace_id=workspace_id,
        account_id=account_id,
        invited_by=actor_id(actor),
    ):
        raise LeaderAlreadyInvited(account_id)
    count, _changed = _replace_projection(
        repository,
        workspace_id=workspace_id,
        owner_id=row["owner_id"],
        dependencies=dependencies,
    )
    final = repository.bump_version(workspace_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_id(actor),
        action="workspace.leader.invited",
        workspace_id=workspace_id,
        reason=f"{reason}; account={account_id}; version={final['version']}",
    )
    _audit_projection(
        repository,
        dependencies=dependencies,
        workspace_id=workspace_id,
        count=count,
        version=final["version"],
    )
    return workspace_dto(final)


def remove_leader(
    repository: WorkspaceRepository,
    *,
    workspace_id: str,
    account_id: str,
    expected_version: int,
    actor: Any,
    reason: str,
    dependencies: WorkspaceDependencies,
) -> WorkspaceDto:
    row = _locked_owned_workspace(
        repository,
        workspace_id=workspace_id,
        expected_version=expected_version,
        actor=actor,
    )
    if account_id == row["owner_id"]:
        raise OwnerCannotBeRemoved(account_id)
    if not repository.delete_leader(workspace_id=workspace_id, account_id=account_id):
        raise LeaderNotInvited(account_id)
    count, _changed = _replace_projection(
        repository,
        workspace_id=workspace_id,
        owner_id=row["owner_id"],
        dependencies=dependencies,
    )
    final = repository.bump_version(workspace_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_id(actor),
        action="workspace.leader.removed",
        workspace_id=workspace_id,
        reason=f"{reason}; account={account_id}; version={final['version']}",
    )
    _audit_projection(
        repository,
        dependencies=dependencies,
        workspace_id=workspace_id,
        count=count,
        version=final["version"],
    )
    return workspace_dto(final)


def transfer_owner(
    repository: WorkspaceRepository,
    *,
    workspace_id: str,
    new_owner_id: str,
    expected_version: int,
    actor: Any,
    reason: str,
    dependencies: WorkspaceDependencies,
) -> WorkspaceDto:
    row = _locked_owned_workspace(
        repository,
        workspace_id=workspace_id,
        expected_version=expected_version,
        actor=actor,
    )
    if new_owner_id not in repository.leader_ids(workspace_id):
        raise LeaderNotInvited(new_owner_id)
    if not _valid_leader(new_owner_id, dependencies):
        raise InvalidWorkspaceParticipant("new owner must remain an effective leader")
    old_owner = row["owner_id"]
    repository.update_owner(workspace_id=workspace_id, owner_id=new_owner_id)
    count, _changed = _replace_projection(
        repository,
        workspace_id=workspace_id,
        owner_id=new_owner_id,
        dependencies=dependencies,
    )
    final = repository.bump_version(workspace_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_id(actor),
        action="workspace.owner.transferred",
        workspace_id=workspace_id,
        reason=(
            f"{reason}; oldOwner={old_owner}; newOwner={new_owner_id}; version={final['version']}"
        ),
    )
    _audit_projection(
        repository,
        dependencies=dependencies,
        workspace_id=workspace_id,
        count=count,
        version=final["version"],
    )
    return workspace_dto(final)
