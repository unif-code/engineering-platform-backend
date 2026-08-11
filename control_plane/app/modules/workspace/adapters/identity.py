from sqlalchemy import Engine

from control_plane.app.modules.identity import IdentityDependencies, get_organization_account
from control_plane.app.modules.workspace.ports import WorkspaceAccountView


class SqlAlchemyIdentityAccountLookup:
    def __init__(self, engine: Engine, dependencies: IdentityDependencies) -> None:
        self.engine = engine
        self.dependencies = dependencies

    def get(self, account_id: str) -> WorkspaceAccountView | None:
        with self.engine.connect() as db:
            account = get_organization_account(
                db,
                account_id=account_id,
                dependencies=self.dependencies,
            )
        if account is None:
            return None
        return WorkspaceAccountView(
            id=account.id,
            status=account.status.value,
            initialized=account.initialized,
        )
