import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_workspace_database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    owner_url = make_url(os.environ["MIGRATION_DATABASE_URL"])
    database_name = f"task8_workspace_{uuid4().hex}"
    maintenance = create_engine(owner_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with maintenance.connect() as db:
            db.execute(text(f'CREATE DATABASE "{database_name}"'))
    except SQLAlchemyError:
        maintenance.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            pytest.fail("Required PostgreSQL unavailable for fresh workspace migration test")
        pytest.skip("PostgreSQL unavailable for fresh workspace migration test")
    target_url = owner_url.set(database=database_name).render_as_string(hide_password=False)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", target_url)
    try:
        yield target_url
    finally:
        with maintenance.connect() as db:
            db.execute(text(f'DROP DATABASE "{database_name}"'))
        maintenance.dispose()


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def test_fresh_upgrade_heads_installs_workspace_and_independent_graph(
    fresh_workspace_database_url: str,
) -> None:
    config = _config(fresh_workspace_database_url)
    command.upgrade(config, "heads")
    engine = create_engine(fresh_workspace_database_url)
    try:
        expected_heads = set(ScriptDirectory.from_config(config).get_heads())
        with engine.connect() as db:
            installed_heads = set(
                db.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
        assert expected_heads == {
            "0006_audit_configuration_grant",
            "0010_identity_policy_reauth",
            "0001_organization_base",
            "0001_workspace_base",
            "0004_authorization_pending_set",
        }
        assert installed_heads == {
            "0006_audit_configuration_grant",
            "0010_identity_policy_reauth",
            "0004_authorization_pending_set",
        }
        assert set(inspect(engine).get_table_names(schema="workspace")) == {
            "idempotency_record",
            "leader",
            "members_projection",
            "workspace",
        }
    finally:
        engine.dispose()


def test_workspace_upgrade_preserves_existing_identity_audit_and_organization_data(
    fresh_workspace_database_url: str,
) -> None:
    config = _config(fresh_workspace_database_url)
    command.upgrade(config, "0003_audit_org_append_grant")
    command.upgrade(config, "0004_identity_bootstrap_totp_cap")
    engine = create_engine(fresh_workspace_database_url)
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO identity.account "
                    "(id, employee_no, display_name, status) VALUES "
                    "('00000000-0000-0000-0000-000000000831', "
                    "'00000831', 'Existing account', 'PENDING_INIT')"
                )
            )
            db.execute(
                text(
                    "INSERT INTO organization.org_edge (account_id, superior_id, kind) "
                    "VALUES ('00000000-0000-0000-0000-000000000831', NULL, 'MANAGER')"
                )
            )
            db.execute(
                text(
                    "INSERT INTO audit.audit_event "
                    "(id, occurred_at, actor, actor_type, action, target_type, "
                    "target_id, result, correlation_id, schema_version) VALUES "
                    "('00000000-0000-0000-0000-000000000832', now(), 'SYSTEM', "
                    "'SYSTEM', 'existing.event', 'ACCOUNT', 'existing', 'SUCCESS', "
                    "'task8-preexisting', 1)"
                )
            )
        command.upgrade(config, "heads")
        with engine.connect() as db:
            values = db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM identity.account WHERE id="
                    "'00000000-0000-0000-0000-000000000831'), "
                    "(SELECT count(*) FROM organization.org_edge WHERE account_id="
                    "'00000000-0000-0000-0000-000000000831'), "
                    "(SELECT count(*) FROM audit.audit_event WHERE id="
                    "'00000000-0000-0000-0000-000000000832')"
                )
            ).one()
        assert values == (1, 1, 1)
    finally:
        engine.dispose()


def test_all_module_migrations_round_trip_on_exact_random_database(
    fresh_workspace_database_url: str,
) -> None:
    config = _config(fresh_workspace_database_url)
    command.upgrade(config, "heads")
    command.downgrade(config, "base")
    engine = create_engine(fresh_workspace_database_url)
    try:
        assert "workspace" not in inspect(engine).get_schema_names()
        command.upgrade(config, "heads")
        assert set(inspect(engine).get_table_names(schema="workspace")) == {
            "idempotency_record",
            "leader",
            "members_projection",
            "workspace",
        }
    finally:
        engine.dispose()
