from sqlalchemy import Engine

from control_plane.app.modules.identity import (
    IdentityDependencies,
    get_organization_account,
)
from control_plane.app.modules.organization.ports import OrganizationAccountView


class SqlAlchemyIdentityAccountLookup:
    """Organization adapter that consumes identity only through its public Facade."""

    def __init__(self, engine: Engine, dependencies: IdentityDependencies) -> None:
        self.engine = engine
        self.dependencies = dependencies

    def get(self, account_id: str) -> OrganizationAccountView | None:
        with self.engine.connect() as db:
            account = get_organization_account(
                db,
                account_id=account_id,
                dependencies=self.dependencies,
            )
        if account is None:
            return None
        return OrganizationAccountView(
            id=account.id,
            employee_no=account.employee_no,
            display_name=account.display_name,
            status=account.status.value,
            initialized=account.initialized,
        )
