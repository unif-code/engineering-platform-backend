from dataclasses import dataclass

from sqlalchemy import Engine

from control_plane.app.modules.authorization import (
    AuthorizationDependencies,
    Scope,
    effective_grants,
)
from control_plane.app.modules.identity import (
    AccountStatus,
    IdentityDependencies,
    get_account,
)
from control_plane.app.modules.requirement.ports import GatePolicySnapshot
from control_plane.app.modules.workspace import (
    WorkspaceDependencies,
    get_workspace,
    is_formal_member,
)

_POLICY_CODE = "REQUIREMENT_BASELINE_WORKSPACE_OWNER"
_POLICY_HASH = "sha256:bdfadcc2d2c32fdb9fdf327d45a231cd2e5cb9bf3028f4e09d527fdb50dd8ea2"
_DECIDE_CAPABILITY = "requirement.baseline.decide"


@dataclass(frozen=True, slots=True)
class WorkspaceOwnerGatePolicy:
    workspace_engine: Engine
    workspace_dependencies: WorkspaceDependencies

    def requirement_baseline(self, *, workspace_id: str) -> GatePolicySnapshot:
        with self.workspace_engine.connect() as db:
            workspace = get_workspace(
                db,
                workspace_id=workspace_id,
                dependencies=self.workspace_dependencies,
            )
        return GatePolicySnapshot(
            version=1,
            default_reviewer_id=workspace.owner_id,
            policy_code=_POLICY_CODE,
            snapshot_hash=_POLICY_HASH,
        )


@dataclass(frozen=True, slots=True)
class ComposedGateReviewerGuard:
    identity_engine: Engine
    identity_dependencies: IdentityDependencies
    workspace_engine: Engine
    workspace_dependencies: WorkspaceDependencies
    authorization_engine: Engine
    authorization_dependencies: AuthorizationDependencies

    def can_decide(self, *, actor_id: str, workspace_id: str) -> bool:
        with self.identity_engine.connect() as db:
            account = get_account(
                db,
                account_id=actor_id,
                dependencies=self.identity_dependencies,
            )
        if account.status is not AccountStatus.ENABLED:
            return False
        with self.workspace_engine.connect() as db:
            if not is_formal_member(
                db,
                workspace_id=workspace_id,
                account_id=actor_id,
                dependencies=self.workspace_dependencies,
            ):
                return False
        with self.authorization_engine.connect() as db:
            grants = effective_grants(
                db,
                principal_id=actor_id,
                capability=_DECIDE_CAPABILITY,
                scope=Scope.workspace(workspace_id),
                dependencies=self.authorization_dependencies,
            )
        return bool(grants)
