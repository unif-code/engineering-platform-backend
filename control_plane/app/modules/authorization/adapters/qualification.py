from dataclasses import dataclass

from sqlalchemy import Engine

import control_plane.app.modules.identity as identity
import control_plane.app.modules.workspace as workspace
from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
)
from control_plane.app.modules.authorization.application.qualification import evaluate
from control_plane.app.modules.authorization.domain.qualification import (
    ActorAccountFacts,
    ActorQualificationSnapshot,
    ActorWorkspaceFacts,
)
from control_plane.app.modules.authorization.ports.qualification import ActorFactsPort


@dataclass(frozen=True, slots=True)
class CurrentActorFactsAdapter:
    identity_engine: Engine
    identity_dependencies: identity.IdentityDependencies
    workspace_engine: Engine
    workspace_dependencies: workspace.WorkspaceDependencies

    def account(self, actor_id: str) -> ActorAccountFacts:
        with self.identity_engine.connect() as db:
            before = identity.get_account(
                db, account_id=actor_id, dependencies=self.identity_dependencies
            )
            initialized = identity.get_organization_account(
                db, account_id=actor_id, dependencies=self.identity_dependencies
            )
            after = identity.get_account(
                db, account_id=actor_id, dependencies=self.identity_dependencies
            )
        if before != after or initialized is None or initialized.status != after.status:
            raise ValueError("Identity facts changed")
        return ActorAccountFacts(
            account_id=after.id,
            version=after.version,
            status=after.status.value,
            initialized=initialized.initialized,
        )

    def membership(self, actor_id: str, workspace_id: str) -> ActorWorkspaceFacts:
        with self.workspace_engine.connect() as db:
            before = workspace.get_workspace(
                db, workspace_id=workspace_id, dependencies=self.workspace_dependencies
            )
            members = workspace.members(
                db, workspace_id=workspace_id, dependencies=self.workspace_dependencies
            )
            after = workspace.get_workspace(
                db, workspace_id=workspace_id, dependencies=self.workspace_dependencies
            )
        if before != after:
            raise ValueError("Workspace facts changed")
        relevant = [member for member in members if member.account_id == actor_id]
        if len(relevant) > 1:
            raise ValueError("Ambiguous membership facts")
        member = relevant[0] if relevant else None
        return ActorWorkspaceFacts(
            workspace_id=after.id,
            version=after.version,
            member_source=member.source.value if member else None,
            member_computed_at=member.computed_at if member else None,
            archived=after.archived_at is not None,
        )


@dataclass(frozen=True, slots=True)
class ActorQualificationRuntime:
    engine: Engine
    dependencies: AuthorizationDependencies
    facts: ActorFactsPort

    def evaluate(
        self,
        actor_id: str,
        workspace_id: str,
        required_capabilities: tuple[str, ...],
    ) -> ActorQualificationSnapshot:
        with self.engine.connect() as db:
            return evaluate(
                self.dependencies.repository_factory(db),
                actor_id=actor_id,
                workspace_id=workspace_id,
                required_capabilities=required_capabilities,
                facts=self.facts,
                dependencies=self.dependencies,
            )
