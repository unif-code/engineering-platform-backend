from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration


def test_authorization_schema_is_absent_before_task_9_migration(
    authorization_owner_engine: Engine,
) -> None:
    """RED: adding the migration must make the required schema/tables exist."""
    inspector = inspect(authorization_owner_engine)

    assert set(inspector.get_table_names(schema="authorization")) == {
        "grant",
        "idempotency_record",
        "principal_version",
        "route_registry",
    }


def test_authorization_tables_enforce_grant_and_fence_invariants(
    authorization_owner_engine: Engine,
) -> None:
    with authorization_owner_engine.connect() as db:
        transaction = db.begin()
        try:
            with pytest.raises(IntegrityError):
                db.execute(
                    text(
                        'INSERT INTO "authorization"."grant" '
                        "(id, principal_id, capability, scope_type, scope_id, source, status, "
                        "version, created_at, updated_at) VALUES "
                        "('00000000-0000-0000-0000-000000000901', 'p', 'cap', "
                        "'PLATFORM', 'not-null', 'MANUAL', 'ACTIVE', 1, now(), now())"
                    )
                )
        finally:
            transaction.rollback()


def test_product_migration_contains_no_runtime_login_secret() -> None:
    source = Path("migrations/authorization/0001_authorization_base.py").read_text(encoding="utf-8")
    assert "LOGIN PASSWORD" not in source.upper()
    assert "'localdev'" not in source


def test_authorization_runtime_role_is_least_privilege(
    authorization_rw_engine: Engine,
) -> None:
    expected = {
        "grant": {"SELECT", "INSERT", "UPDATE"},
        "principal_version": {"SELECT", "INSERT", "UPDATE"},
        "route_registry": {"SELECT"},
        "idempotency_record": {"SELECT", "INSERT", "UPDATE"},
    }
    with authorization_rw_engine.connect() as db:
        actual = {
            table_name: {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if db.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'authorization_rw', :qualified_name, :privilege)"
                    ),
                    {
                        "qualified_name": f'"authorization"."{table_name}"',
                        "privilege": privilege,
                    },
                ).scalar_one()
            }
            for table_name in expected
        }
        audit_dml = db.execute(
            text("SELECT has_table_privilege('authorization_rw', 'audit.audit_event', 'INSERT')")
        ).scalar_one()
    assert actual == expected
    assert audit_dml is False
