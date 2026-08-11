from typing import Any

from sqlalchemy import Engine

import control_plane.app.modules.organization as organization
import control_plane.app.modules.workspace as workspace


class SqlAlchemyWorkspaceMembership:
    def __init__(
        self,
        engine: Engine,
        dependencies: workspace.WorkspaceDependencies,
    ) -> None:
        self.engine = engine
        self.dependencies = dependencies

    def is_formal_member(self, workspace_id: str, account_id: str) -> bool:
        with self.engine.connect() as db:
            return workspace.is_formal_member(
                db,
                workspace_id=workspace_id,
                account_id=account_id,
                dependencies=self.dependencies,
            )


class SqlAlchemyOrganizationSummary:
    def __init__(
        self,
        engine: Engine,
        dependencies: organization.OrganizationDependencies,
    ) -> None:
        self.engine = engine
        self.dependencies = dependencies

    def __call__(self, account_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as db:
            tree = organization.get_tree(db, dependencies=self.dependencies)
        for manager in tree.managers:
            if manager.account.id == account_id:
                return {"accountId": account_id, "kind": "MANAGER", "superiorId": None}
            for leader in manager.leaders:
                if leader.account.id == account_id:
                    return {
                        "accountId": account_id,
                        "kind": "LEADER",
                        "superiorId": manager.account.id,
                    }
                for member in leader.members:
                    if member.id == account_id:
                        return {
                            "accountId": account_id,
                            "kind": "MEMBER",
                            "superiorId": leader.account.id,
                        }
        return None


class SqlAlchemyWorkspaceSummaries:
    def __init__(
        self,
        engine: Engine,
        dependencies: workspace.WorkspaceDependencies,
    ) -> None:
        self.engine = engine
        self.dependencies = dependencies

    def __call__(self, account_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as db:
            values = workspace.list_workspaces(db, dependencies=self.dependencies)
        result: list[dict[str, Any]] = []
        membership = SqlAlchemyWorkspaceMembership(self.engine, self.dependencies)
        for value in values:
            if membership.is_formal_member(value.id, account_id):
                result.append(
                    {
                        "id": value.id,
                        "name": value.name,
                        "ownerId": value.owner_id,
                    }
                )
        return result
