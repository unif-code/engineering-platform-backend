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
from tests.integration_database import parse_database_url
from tests.workspace.conftest import _temporary_workspace_role_engine

pytestmark = pytest.mark.integration


def test_workspace_schema_tables_and_runtime_role_exist(
    workspace_owner_engine: Engine,
) -> None:
    inspector = inspect(workspace_owner_engine)

    assert set(inspector.get_table_names(schema="workspace")) == {
        "idempotency_record",
        "leader",
        "members_projection",
        "workspace",
    }
    with workspace_owner_engine.connect() as db:
        role_exists = db.execute(
            text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname='workspace_rw')")
        ).scalar_one()
    assert role_exists is True


def _emitted_runtime_role_statement(monkeypatch: pytest.MonkeyPatch) -> str:
    statements: list[str] = []
    monkeypatch.setattr(op, "execute", lambda statement: statements.append(str(statement)))
    migration = run_path("migrations/workspace/0001_workspace_base.py")
    migration["upgrade"]()
    return next(statement for statement in statements if "CREATE ROLE" in statement)


def test_product_migration_contains_no_runtime_login_secret() -> None:
    source = Path("migrations/workspace/0001_workspace_base.py").read_text(encoding="utf-8")

    assert "LOGIN PASSWORD" not in source.upper()
    assert "'localdev'" not in source


def test_missing_runtime_role_is_provisioned_as_nologin(
    monkeypatch: pytest.MonkeyPatch,
    workspace_owner_engine: Engine,
) -> None:
    role_name = f"task8_workspace_missing_{uuid4().hex}"
    statement = _emitted_runtime_role_statement(monkeypatch).replace("workspace_rw", role_name)

    try:
        with workspace_owner_engine.begin() as db:
            db.execute(text(statement))
            can_login = db.execute(
                text("SELECT rolcanlogin FROM pg_roles WHERE rolname=:role_name"),
                {"role_name": role_name},
            ).scalar_one()
        assert can_login is False
    finally:
        with workspace_owner_engine.begin() as db:
            db.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))


def test_preprovisioned_login_role_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    workspace_owner_engine: Engine,
) -> None:
    role_name = f"task8_workspace_prebuilt_{uuid4().hex}"
    statement = _emitted_runtime_role_statement(monkeypatch).replace("workspace_rw", role_name)

    try:
        with workspace_owner_engine.begin() as db:
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
        with workspace_owner_engine.begin() as db:
            db.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))


def test_runtime_fixture_uses_disposable_login_without_rewriting_shared_role(
    workspace_owner_engine: Engine,
) -> None:
    with workspace_owner_engine.connect() as db:
        before = db.execute(
            text("SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname='workspace_rw'")
        ).one()

    with _temporary_workspace_role_engine(
        workspace_owner_engine,
        parse_database_url(
            DbSettings().workspace_database_url,
            setting_name="WORKSPACE_DATABASE_URL",
        ),
    ) as (runtime_engine, login_role):
        with runtime_engine.connect() as db:
            current_role, session_role = db.execute(text("SELECT current_user, session_user")).one()
        assert current_role == "workspace_rw"
        assert session_role == login_role

    with workspace_owner_engine.connect() as db:
        after = db.execute(
            text("SELECT rolcanlogin, rolpassword FROM pg_authid WHERE rolname='workspace_rw'")
        ).one()
        temporary_role_exists = db.execute(
            text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname=:login_role)"),
            {"login_role": login_role},
        ).scalar_one()
    assert after == before
    assert temporary_role_exists is False


def test_runtime_fixture_invalid_url_does_not_leak_temporary_login(
    workspace_owner_engine: Engine,
) -> None:
    with workspace_owner_engine.connect() as db:
        before = set(
            db.execute(
                text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'test_workspace_login_%'")
            ).scalars()
        )
    with pytest.raises(
        pytest.fail.Exception,
        match="TEST_WORKSPACE_DATABASE_URL must be a valid SQLAlchemy database URL",
    ):
        parse_database_url(
            "test-only-malformed-workspace-database-url",
            setting_name="TEST_WORKSPACE_DATABASE_URL",
        )
    with workspace_owner_engine.connect() as db:
        after = set(
            db.execute(
                text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'test_workspace_login_%'")
            ).scalars()
        )
    assert after == before


def test_workspace_tables_use_stable_ids_and_only_intraschema_foreign_keys(
    workspace_owner_engine: Engine,
) -> None:
    inspector = inspect(workspace_owner_engine)
    workspace_columns = {
        column["name"]: (type(column["type"]).__name__, column["nullable"])
        for column in inspector.get_columns("workspace", schema="workspace")
    }
    assert workspace_columns == {
        "id": ("UUID", False),
        "name": ("TEXT", False),
        "owner_id": ("TEXT", False),
        "archived_at": ("TIMESTAMP", True),
        "version": ("INTEGER", False),
    }
    for table_name in ("workspace", "leader", "members_projection"):
        assert all(
            foreign_key["referred_schema"] in (None, "workspace")
            for foreign_key in inspector.get_foreign_keys(table_name, schema="workspace")
        )


@contextmanager
def _rollback(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as db:
        transaction = db.begin()
        try:
            yield db
        finally:
            transaction.rollback()


def test_workspace_constraints_reject_invalid_facts(
    workspace_owner_engine: Engine,
) -> None:
    with _rollback(workspace_owner_engine) as db, pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO workspace.workspace (id, name, owner_id, version) "
                "VALUES ('00000000-0000-0000-0000-000000000821', ' ', 'owner', 1)"
            )
        )
    with _rollback(workspace_owner_engine) as db, pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO workspace.workspace (id, name, owner_id, version) "
                "VALUES ('00000000-0000-0000-0000-000000000822', 'Name', 'owner', 0)"
            )
        )
    with _rollback(workspace_owner_engine) as db:
        db.execute(
            text(
                "INSERT INTO workspace.workspace (id, name, owner_id) "
                "VALUES ('00000000-0000-0000-0000-000000000823', 'Name', 'owner')"
            )
        )
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO workspace.members_projection "
                    "(workspace_id, account_id, source, computed_at) VALUES "
                    "('00000000-0000-0000-0000-000000000823', 'account', 'UNKNOWN', now())"
                )
            )


def test_workspace_idempotency_scope_and_state_are_constrained(
    workspace_owner_engine: Engine,
) -> None:
    statement = text(
        "INSERT INTO workspace.idempotency_record "
        "(id, actor, operation, idempotency_key, request_fingerprint, state) VALUES "
        "(:id, 'actor', 'workspace_create', 'same-key', 'fingerprint', 'IN_PROGRESS')"
    )
    with _rollback(workspace_owner_engine) as db:
        db.execute(statement, {"id": "00000000-0000-0000-0000-000000000824"})
        with pytest.raises(IntegrityError):
            db.execute(statement, {"id": "00000000-0000-0000-0000-000000000825"})
    with _rollback(workspace_owner_engine) as db, pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO workspace.idempotency_record "
                "(id, actor, operation, idempotency_key, request_fingerprint, state, "
                "http_status) VALUES "
                "('00000000-0000-0000-0000-000000000826', 'actor', "
                "'workspace_create', 'another-key', 'fingerprint', 'IN_PROGRESS', 200)"
            )
        )


def test_workspace_rw_has_minimal_module_and_audit_privileges(
    workspace_rw_engine: Engine,
    workspace_owner_engine: Engine,
) -> None:
    expected = {
        "workspace": {"SELECT", "INSERT", "UPDATE"},
        "idempotency_record": {"SELECT", "INSERT", "UPDATE"},
        "leader": {"SELECT", "INSERT", "DELETE"},
        "members_projection": {"SELECT", "INSERT", "DELETE"},
    }
    with workspace_rw_engine.connect() as db:
        actual = {
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
                        "'workspace_rw', 'workspace.' || :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
            }
            for table_name in expected
        }
        schema_privileges = {
            privilege
            for privilege in ("USAGE", "CREATE")
            if db.execute(
                text("SELECT has_schema_privilege('workspace_rw', 'workspace', :privilege)"),
                {"privilege": privilege},
            ).scalar_one()
        }
    with workspace_owner_engine.connect() as db:
        cross_module = {
            table_name: db.execute(
                text("SELECT has_table_privilege('workspace_rw', :table_name, 'SELECT')"),
                {"table_name": table_name},
            ).scalar_one()
            for table_name in ("identity.account", "organization.org_edge", "audit.audit_event")
        }
        function_execute = db.execute(
            text(
                "SELECT has_function_privilege("
                "'workspace_rw', "
                "'audit.append_event(text,timestamptz,text,text,text,text,text,text,"
                "text,text,integer)', "
                "'EXECUTE')"
            )
        ).scalar_one()
    assert actual == expected
    assert schema_privileges == {"USAGE"}
    assert cross_module == {
        "identity.account": False,
        "organization.org_edge": False,
        "audit.audit_event": False,
    }
    assert function_execute is True


def test_workspace_rw_cannot_delete_workspace_or_create_runtime_tables(
    workspace_rw_engine: Engine,
) -> None:
    with (
        workspace_rw_engine.connect() as db,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        db.execute(text("DELETE FROM workspace.workspace WHERE false"))
    with (
        workspace_rw_engine.connect() as db,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        db.execute(text("CREATE TABLE workspace.runtime_ddl_forbidden (id int)"))


def test_workspace_rw_audit_append_commits_and_rolls_back_with_domain_transaction(
    workspace_rw_engine: Engine,
    workspace_owner_engine: Engine,
) -> None:
    statement = text(
        "SELECT audit.append_event("
        ":id, now(), 'actor', 'HUMAN', 'workspace.test', 'WORKSPACE', 'target', "
        "'SUCCESS', 'stable summary', :request_id, 1)"
    )
    with workspace_rw_engine.begin() as db:
        db.execute(
            statement,
            {"id": "00000000-0000-0000-0000-000000000827", "request_id": "request-827"},
        )
    with workspace_rw_engine.connect() as db:
        transaction = db.begin()
        db.execute(
            statement,
            {"id": "00000000-0000-0000-0000-000000000828", "request_id": "request-828"},
        )
        transaction.rollback()
    with workspace_owner_engine.connect() as db:
        committed = (
            db.execute(
                text(
                    "SELECT id FROM audit.audit_event "
                    "WHERE id IN ('00000000-0000-0000-0000-000000000827', "
                    "'00000000-0000-0000-0000-000000000828') ORDER BY id"
                )
            )
            .scalars()
            .all()
        )
    assert committed == ["00000000-0000-0000-0000-000000000827"]


def test_workspace_runtime_settings_have_a_distinct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "postgresql+psycopg://workspace_rw:localdev@127.0.0.1:55432/platform"
    monkeypatch.setenv("WORKSPACE_DATABASE_URL", expected)
    settings = DbSettings()
    assert settings.workspace_database_url == expected
    assert settings.workspace_database_url not in {
        settings.database_url,
        settings.identity_database_url,
        settings.organization_database_url,
        settings.migration_database_url,
    }
