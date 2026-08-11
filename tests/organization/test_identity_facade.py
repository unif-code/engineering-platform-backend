import pytest
from sqlalchemy import Engine, text

import control_plane.app.modules.identity as identity
from control_plane.app.modules.organization.adapters.identity import (
    SqlAlchemyIdentityAccountLookup,
)
from tests.identity.task5_helpers import dependencies
from tests.organization.helpers import insert_account

pytestmark = pytest.mark.integration


def test_identity_facade_returns_only_safe_organization_account_fields(
    organization_owner_engine: Engine,
    clean_organization_db: None,
) -> None:
    account_id = "00000000-0000-0000-0000-000000000101"
    with organization_owner_engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO identity.account "
                "(id, employee_no, display_name, profession, status) VALUES "
                "(:id, '00000101', 'Alice', 'backend', 'ENABLED')"
            ),
            {"id": account_id},
        )

    query = getattr(identity, "get_organization_account", lambda *_args, **_kwargs: None)
    with organization_owner_engine.connect() as db:
        result = query(db, account_id=account_id, dependencies=dependencies())

    assert result is not None
    assert result.model_dump(mode="json") == {
        "id": account_id,
        "employee_no": "00000101",
        "display_name": "Alice",
        "status": "ENABLED",
        "initialized": False,
    }


def test_organization_identity_adapter_uses_the_public_facade(
    organization_owner_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    account_id = "00000000-0000-0000-0000-000000000102"
    insert_account(
        organization_owner_engine,
        account_id=account_id,
        employee_no="00000102",
        display_name="Bob",
    )
    adapter = SqlAlchemyIdentityAccountLookup(
        organization_identity_engine,
        dependencies(),
    )

    projection = adapter.get(account_id)

    assert projection is not None
    assert projection.id == account_id
    assert projection.status == identity.AccountStatus.ENABLED
    assert projection.initialized is True
