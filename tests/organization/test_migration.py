from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from runpy import run_path
from uuid import uuid4

import pytest
from alembic import op
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from control_plane.app.shared.db.settings import DbSettings

pytestmark = pytest.mark.integration


def _emitted_runtime_role_statement(monkeypatch: pytest.MonkeyPatch) -> str:
    statements: list[str] = []
    monkeypatch.setattr(op, "execute", lambda statement: statements.append(str(statement)))
    migration = run_path("migrations/organization/0001_org_base.py")
    migration["upgrade"]()
    return next(statement for statement in statements if "CREATE ROLE" in statement)


def test_product_migration_contains_no_runtime_login_secret() -> None:
    source = Path("migrations/organization/0001_org_base.py").read_text(encoding="utf-8")

    assert "LOGIN PASSWORD" not in source.upper()
    assert "'localdev'" not in source


def test_missing_runtime_role_is_provisioned_as_nologin(
    monkeypatch: pytest.MonkeyPatch,
    organization_owner_engine: Engine,
) -> None:
    role_name = f"task7_org_missing_{uuid4().hex}"
    statement = _emitted_runtime_role_statement(monkeypatch).replace("organization_rw", role_name)

    try:
        with organization_owner_engine.begin() as db:
            db.execute(text(statement))
            can_login = db.execute(
                text("SELECT rolcanlogin FROM pg_roles WHERE rolname=:role_name"),
                {"role_name": role_name},
            ).scalar_one()
        assert can_login is False
    finally:
        with organization_owner_engine.begin() as db:
            db.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))


def test_preprovisioned_login_role_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    organization_owner_engine: Engine,
) -> None:
    role_name = f"task7_org_prebuilt_{uuid4().hex}"
    statement = _emitted_runtime_role_statement(monkeypatch).replace("organization_rw", role_name)

    try:
        with organization_owner_engine.begin() as db:
            db.execute(text(f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD 'test-only-password'"))
            before = db.execute(
                text("SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname=:role_name"),
                {"role_name": role_name},
            ).one()
            db.execute(text(statement))
            after = db.execute(
                text("SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname=:role_name"),
                {"role_name": role_name},
            ).one()
        assert before[0] is True
        assert after == before
    finally:
        with organization_owner_engine.begin() as db:
            db.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))


def test_organization_schema_tables_and_runtime_role_exist(
    organization_owner_engine: Engine,
) -> None:
    inspector = inspect(organization_owner_engine)

    assert set(inspector.get_table_names(schema="organization")) == {
        "idempotency_record",
        "org_edge",
    }
    with organization_owner_engine.connect() as db:
        role_exists = db.execute(
            text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname='organization_rw')")
        ).scalar_one()
    assert role_exists is True


def test_org_edge_uses_stable_ids_without_cross_schema_foreign_keys(
    organization_owner_engine: Engine,
) -> None:
    inspector = inspect(organization_owner_engine)

    columns = {
        column["name"]: (type(column["type"]).__name__, column["nullable"])
        for column in inspector.get_columns("org_edge", schema="organization")
    }
    foreign_keys = inspector.get_foreign_keys("org_edge", schema="organization")

    assert columns == {
        "account_id": ("TEXT", False),
        "superior_id": ("TEXT", True),
        "kind": ("TEXT", False),
        "created_at": ("TIMESTAMP", False),
        "updated_at": ("TIMESTAMP", False),
    }
    assert foreign_keys == []


@contextmanager
def _rollback(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as db:
        transaction = db.begin()
        try:
            yield db
        finally:
            transaction.rollback()


@pytest.mark.parametrize(
    ("account_id", "superior_id", "kind"),
    [
        ("manager", "somebody", "MANAGER"),
        ("leader", None, "LEADER"),
        ("member", None, "MEMBER"),
        ("self", "self", "LEADER"),
        ("unknown", None, "UNKNOWN"),
    ],
)
def test_org_edge_constraints_reject_invalid_fixed_levels(
    organization_owner_engine: Engine,
    account_id: str,
    superior_id: str | None,
    kind: str,
) -> None:
    with _rollback(organization_owner_engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO organization.org_edge "
                    "(account_id, superior_id, kind) VALUES (:account_id, :superior_id, :kind)"
                ),
                {"account_id": account_id, "superior_id": superior_id, "kind": kind},
            )


def test_organization_idempotency_scope_and_state_are_constrained(
    organization_owner_engine: Engine,
) -> None:
    with _rollback(organization_owner_engine) as db:
        statement = text(
            "INSERT INTO organization.idempotency_record "
            "(id, actor, operation, idempotency_key, request_fingerprint, state) VALUES "
            "(:id, 'actor', 'org_set_superior', 'same-key', 'fingerprint', 'IN_PROGRESS')"
        )
        db.execute(statement, {"id": "00000000-0000-0000-0000-000000000001"})
        with pytest.raises(IntegrityError):
            db.execute(statement, {"id": "00000000-0000-0000-0000-000000000002"})

    with _rollback(organization_owner_engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO organization.idempotency_record "
                    "(id, actor, operation, idempotency_key, request_fingerprint, state, "
                    "http_status) VALUES "
                    "('00000000-0000-0000-0000-000000000003', 'actor', "
                    "'org_set_superior', 'another-key', 'fingerprint', 'IN_PROGRESS', 200)"
                )
            )


def test_organization_rw_has_minimal_module_privileges(
    organization_rw_engine: Engine,
    organization_owner_engine: Engine,
) -> None:
    with organization_rw_engine.connect() as db:
        schema_privileges = {
            privilege
            for privilege in ("USAGE", "CREATE")
            if db.execute(
                text("SELECT has_schema_privilege('organization_rw', 'organization', :value)"),
                {"value": privilege},
            ).scalar_one()
        }
        table_privileges = {
            table_name: {
                privilege
                for privilege in (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                )
                if db.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'organization_rw', 'organization.' || :table_name, :value)"
                    ),
                    {"table_name": table_name, "value": privilege},
                ).scalar_one()
            }
            for table_name in ("idempotency_record", "org_edge")
        }
    with organization_owner_engine.connect() as db:
        identity_access = db.execute(
            text("SELECT has_table_privilege('organization_rw', 'identity.account', 'SELECT')")
        ).scalar_one()
        audit_dml = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            if db.execute(
                text("SELECT has_table_privilege('organization_rw', 'audit.audit_event', :value)"),
                {"value": privilege},
            ).scalar_one()
        }

    assert schema_privileges == {"USAGE"}
    assert table_privileges == {
        "idempotency_record": {"SELECT", "INSERT", "UPDATE"},
        "org_edge": {"SELECT", "INSERT", "UPDATE"},
    }
    assert identity_access is False
    assert audit_dml == set()


def test_organization_rw_cannot_delete_or_create_runtime_tables(
    organization_rw_engine: Engine,
) -> None:
    with organization_rw_engine.connect() as db:
        with pytest.raises(ProgrammingError, match="permission denied"):
            db.execute(text("DELETE FROM organization.org_edge WHERE false"))
    with organization_rw_engine.connect() as db:
        with pytest.raises(ProgrammingError, match="permission denied"):
            db.execute(text("CREATE TABLE organization.runtime_ddl_forbidden (id int)"))


def test_organization_rw_can_only_append_audit_through_owned_function(
    organization_rw_engine: Engine,
    organization_owner_engine: Engine,
) -> None:
    statement = text(
        "SELECT audit.append_event("
        "'00000000-0000-0000-0000-000000000071', now(), '00000001', 'human', "
        "'organization.structure.changed', 'account', 'target', 'SUCCESS', "
        "'test reason', 'request-71', 1)"
    )
    with organization_rw_engine.begin() as db:
        db.execute(statement)
    with organization_owner_engine.connect() as db:
        committed = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE id='00000000-0000-0000-0000-000000000071'"
            )
        ).scalar_one()
    assert committed == 1

    with organization_rw_engine.connect() as db:
        transaction = db.begin()
        db.execute(
            text(
                "SELECT audit.append_event("
                "'00000000-0000-0000-0000-000000000072', now(), '00000001', 'human', "
                "'organization.structure.changed', 'account', 'target', 'SUCCESS', "
                "'test reason', 'request-72', 1)"
            )
        )
        transaction.rollback()
    with organization_owner_engine.connect() as db:
        rolled_back = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE id='00000000-0000-0000-0000-000000000072'"
            )
        ).scalar_one()
    assert rolled_back == 0


def test_organization_runtime_settings_have_a_distinct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "postgresql+psycopg://organization_rw:localdev@127.0.0.1:55432/platform"
    monkeypatch.setenv("ORGANIZATION_DATABASE_URL", expected)
    settings = DbSettings()

    assert settings.organization_database_url == expected
    assert settings.organization_database_url not in {
        settings.database_url,
        settings.identity_database_url,
        settings.migration_database_url,
    }
