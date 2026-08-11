from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, text

import control_plane.app.modules.identity as identity
import control_plane.app.modules.organization.adapters as organization_adapters
from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.organization import OrganizationDependencies
from control_plane.app.modules.organization.ports import OrganizationAccountView
from control_plane.app.shared.security import SecretMaterial
from tests.identity.task5_helpers import dependencies as identity_dependencies


@dataclass
class FixedClock:
    value: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class RandomValues:
    def uuid4(self) -> object:
        return uuid4()


class StaticSecrets:
    def load(self) -> SecretMaterial:
        return SecretMaterial(b"p" * 32, b"t" * 32, b"i" * 32)


class IdentityFacadeLookup:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.dependencies = identity_dependencies()

    def get(self, account_id: str) -> OrganizationAccountView | None:
        with self.engine.connect() as db:
            account = identity.get_organization_account(
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


def organization_dependencies(
    identity_engine: Engine,
    *,
    on_membership_change: Callable[[Sequence[str]], None],
) -> OrganizationDependencies:
    return OrganizationDependencies(
        repository_factory=organization_adapters.SqlAlchemyOrganizationRepository,
        identity=IdentityFacadeLookup(identity_engine),
        audit=SqlAlchemyTransactionalAuditAppender(),
        on_membership_change=on_membership_change,
        clock=FixedClock(),
        random=RandomValues(),
        secret_manager=StaticSecrets(),
    )


def insert_account(
    owner_engine: Engine,
    *,
    account_id: str,
    employee_no: str,
    display_name: str,
    status: str = "ENABLED",
    initialized: bool = True,
) -> None:
    password_hash = "argon2" if initialized else None
    totp_confirmed_at = datetime(2025, 1, 1, tzinfo=UTC) if initialized else None
    with owner_engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO identity.account "
                "(id, employee_no, display_name, status, password_hash, totp_confirmed_at) "
                "VALUES (:id, :employee_no, :display_name, :status, "
                ":password_hash, :totp_confirmed_at)"
            ),
            {
                "id": account_id,
                "employee_no": employee_no,
                "display_name": display_name,
                "status": status,
                "password_hash": password_hash,
                "totp_confirmed_at": totp_confirmed_at,
            },
        )
