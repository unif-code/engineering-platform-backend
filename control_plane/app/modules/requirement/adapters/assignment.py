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
from control_plane.app.modules.source_control import (
    SourceControlDependencies,
    list_authorized_repositories,
)
from control_plane.app.modules.workspace import (
    WorkspaceDependencies,
    is_formal_member,
)


@dataclass(frozen=True, slots=True)
class ComposedAutomaticAssignmentGuard:
    identity_engine: Engine
    identity_dependencies: IdentityDependencies
    workspace_engine: Engine
    workspace_dependencies: WorkspaceDependencies
    authorization_engine: Engine
    authorization_dependencies: AuthorizationDependencies
    source_control_engine: Engine
    source_control_dependencies: SourceControlDependencies

    def _eligible(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: str,
        required_capabilities: tuple[str, ...],
    ) -> bool:
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

        scope = Scope.workspace(workspace_id)
        with self.authorization_engine.connect() as db:
            for capability in required_capabilities:
                if not effective_grants(
                    db,
                    principal_id=actor_id,
                    capability=capability,
                    scope=scope,
                    dependencies=self.authorization_dependencies,
                ):
                    return False

        with self.source_control_engine.connect() as db:
            repositories = list_authorized_repositories(
                db,
                workspace_id=workspace_id,
                dependencies=self.source_control_dependencies,
            )
        return any(repository.repository_id == repository_id for repository in repositories)

    def can_auto_assign(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: str,
        required_capabilities: tuple[str, ...],
    ) -> bool:
        try:
            return self._eligible(
                actor_id=actor_id,
                workspace_id=workspace_id,
                repository_id=repository_id,
                required_capabilities=required_capabilities,
            )
        except Exception:
            return False

    def can_assign(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: str,
        required_capabilities: tuple[str, ...],
    ) -> bool:
        return self._eligible(
            actor_id=actor_id,
            workspace_id=workspace_id,
            repository_id=repository_id,
            required_capabilities=required_capabilities,
        )
