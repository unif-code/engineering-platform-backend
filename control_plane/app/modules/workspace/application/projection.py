from collections.abc import Sequence

from sqlalchemy import Engine

from control_plane.app.modules.workspace.application.common import audit, is_effective
from control_plane.app.modules.workspace.application.dependencies import WorkspaceDependencies
from control_plane.app.modules.workspace.domain import (
    InvalidWorkspaceParticipant,
    MemberSource,
    WorkspaceArchived,
    WorkspaceNotFound,
)
from control_plane.app.modules.workspace.ports import WorkspaceRepository


def _desired_members(
    repository: WorkspaceRepository,
    *,
    workspace_id: str,
    owner_id: str,
    dependencies: WorkspaceDependencies,
) -> dict[str, str]:
    leader_ids = repository.leader_ids(workspace_id)
    all_leaders = [owner_id, *leader_ids]
    desired: dict[str, str] = {owner_id: MemberSource.OWNER.value}
    for leader_id in leader_ids:
        desired.setdefault(leader_id, MemberSource.LEADER.value)
    for leader_id in all_leaders:
        account = dependencies.identity.get(leader_id)
        if not is_effective(account) or not dependencies.organization.is_effective_leader(
            leader_id
        ):
            raise InvalidWorkspaceParticipant("workspace leader is not effective")
        for report in dependencies.organization.direct_reports(leader_id):
            account = dependencies.identity.get(report.id)
            if is_effective(account):
                desired.setdefault(report.id, MemberSource.DIRECT_REPORT.value)
    return desired


def _replace_projection(
    repository: WorkspaceRepository,
    *,
    workspace_id: str,
    owner_id: str,
    dependencies: WorkspaceDependencies,
) -> tuple[int, bool]:
    desired = _desired_members(
        repository,
        workspace_id=workspace_id,
        owner_id=owner_id,
        dependencies=dependencies,
    )
    existing = {
        str(row["account_id"]): str(row["source"])
        for row in repository.projection_rows(workspace_id)
    }
    changed = existing != desired
    if changed:
        repository.replace_members(
            workspace_id,
            desired,
            computed_at=dependencies.clock.now(),
        )
    return len(desired), changed


def recompute_members(
    repository: WorkspaceRepository,
    *,
    workspace_id: str,
    dependencies: WorkspaceDependencies,
) -> int:
    row = repository.workspace_by_id(workspace_id, for_update=True)
    if row is None:
        raise WorkspaceNotFound(workspace_id)
    if row["archived_at"] is not None:
        raise WorkspaceArchived(workspace_id)
    count, changed = _replace_projection(
        repository,
        workspace_id=workspace_id,
        owner_id=row["owner_id"],
        dependencies=dependencies,
    )
    final = repository.bump_version(workspace_id) if changed else row
    audit(
        repository,
        dependencies=dependencies,
        actor="SYSTEM",
        action="workspace.members.recomputed",
        workspace_id=workspace_id,
        reason=f"count={count}; changed={str(changed).lower()}; version={final['version']}",
    )
    return count


class WorkspaceMembershipChangeHandler:
    """Synchronous Task-8 seam; Task 9 wraps it with the authorization fence."""

    def __init__(self, engine: Engine, dependencies: WorkspaceDependencies) -> None:
        self.engine = engine
        self.dependencies = dependencies

    def __call__(self, account_ids: Sequence[str]) -> None:
        affected = set(account_ids)
        if not affected:
            return
        with self.engine.begin() as db:
            repository = self.dependencies.repository_factory(db)
            for workspace_id in repository.active_workspace_ids():
                row = repository.workspace_by_id(workspace_id, for_update=True)
                if row is None or row["archived_at"] is not None:
                    continue
                desired = _desired_members(
                    repository,
                    workspace_id=workspace_id,
                    owner_id=row["owner_id"],
                    dependencies=self.dependencies,
                )
                existing = {
                    str(member["account_id"]): str(member["source"])
                    for member in repository.projection_rows(workspace_id)
                }
                if not affected.intersection(existing) and not affected.intersection(desired):
                    continue
                changed = existing != desired
                if changed:
                    repository.replace_members(
                        workspace_id,
                        desired,
                        computed_at=self.dependencies.clock.now(),
                    )
                    final = repository.bump_version(workspace_id)
                else:
                    final = row
                audit(
                    repository,
                    dependencies=self.dependencies,
                    actor="SYSTEM",
                    action="workspace.members.recomputed",
                    workspace_id=workspace_id,
                    reason=(
                        f"count={len(desired)}; changed={str(changed).lower()}; "
                        f"version={final['version']}"
                    ),
                )
