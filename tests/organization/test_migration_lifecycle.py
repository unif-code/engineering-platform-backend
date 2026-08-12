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
def fresh_database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    configured = os.environ["MIGRATION_DATABASE_URL"]
    owner_url = make_url(configured)
    database_name = f"task7_organization_{uuid4().hex}"
    maintenance_url = owner_url.set(database="postgres")
    maintenance = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with maintenance.connect() as db:
            db.execute(text(f'CREATE DATABASE "{database_name}"'))
    except SQLAlchemyError:
        maintenance.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            pytest.fail("Required PostgreSQL unavailable for fresh migration test")
        pytest.skip("PostgreSQL unavailable for fresh migration test")
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


def test_fresh_database_upgrade_heads_installs_all_visible_module_heads(
    fresh_database_url: str,
) -> None:
    config = _config(fresh_database_url)
    command.upgrade(config, "heads")
    engine = create_engine(fresh_database_url)
    try:
        expected_heads = set(ScriptDirectory.from_config(config).get_heads())
        with engine.connect() as db:
            installed_heads = set(
                db.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
        assert expected_heads == {
            "0006_audit_configuration_grant",
            "0009_identity_policy_publish",
            "0001_organization_base",
            "0001_workspace_base",
            "0004_authorization_pending_set",
        }
        # Alembic replaces dependency heads in the version table with the revision
        # that depends on them; the organization and workspace heads remain visible
        # in the script graph and their schemas have dedicated lifecycle coverage.
        assert installed_heads == {
            "0006_audit_configuration_grant",
            "0009_identity_policy_publish",
            "0004_authorization_pending_set",
        }
        assert set(inspect(engine).get_table_names(schema="organization")) == {
            "idempotency_record",
            "org_edge",
        }
    finally:
        engine.dispose()


def test_upgrade_adds_organization_without_rewriting_preexisting_module_data(
    fresh_database_url: str,
) -> None:
    config = _config(fresh_database_url)
    command.upgrade(config, "0002_audit_transactional_append")
    command.upgrade(config, "0004_identity_bootstrap_totp_cap")
    engine = create_engine(fresh_database_url)
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO identity.account "
                    "(id, employee_no, display_name, status) VALUES "
                    "('00000000-0000-0000-0000-000000000701', "
                    "'00000701', 'Existing account', 'PENDING_INIT')"
                )
            )
            db.execute(
                text(
                    "INSERT INTO audit.audit_event "
                    "(id, occurred_at, actor, actor_type, action, target_type, "
                    "target_id, result, correlation_id, schema_version) VALUES "
                    "('00000000-0000-0000-0000-000000000702', now(), 'SYSTEM', "
                    "'system', 'existing.event', 'account', 'existing', 'SUCCESS', "
                    "'task7-preexisting', 1)"
                )
            )

        command.upgrade(config, "heads")

        with engine.connect() as db:
            account = db.execute(
                text(
                    "SELECT employee_no, display_name FROM identity.account "
                    "WHERE id='00000000-0000-0000-0000-000000000701'"
                )
            ).one()
            event_count = db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE id='00000000-0000-0000-0000-000000000702'"
                )
            ).scalar_one()
        assert account == ("00000701", "Existing account")
        assert event_count == 1
    finally:
        engine.dispose()


def test_all_module_migrations_round_trip_base_to_heads_on_isolated_database(
    fresh_database_url: str,
) -> None:
    config = _config(fresh_database_url)
    command.upgrade(config, "heads")
    command.downgrade(config, "base")
    engine = create_engine(fresh_database_url)
    try:
        assert "organization" not in inspect(engine).get_schema_names()
        command.upgrade(config, "heads")
        assert set(inspect(engine).get_table_names(schema="organization")) == {
            "idempotency_record",
            "org_edge",
        }
    finally:
        engine.dispose()
