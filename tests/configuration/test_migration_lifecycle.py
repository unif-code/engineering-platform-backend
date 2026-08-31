import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from tests.integration_database import migration_database_url

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_configuration_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[URL]:
    owner_url = migration_database_url()
    database_name = f"task12_configuration_{uuid4().hex}"
    maintenance = create_engine(
        owner_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with maintenance.connect() as db:
            db.execute(text(f'CREATE DATABASE "{database_name}"'))
    except SQLAlchemyError:
        maintenance.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            pytest.fail("Required PostgreSQL unavailable for fresh configuration migration")
        pytest.skip("PostgreSQL unavailable for fresh configuration migration")
    target_url = owner_url.set(database=database_name)
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        target_url.render_as_string(hide_password=False),
    )
    try:
        yield target_url
    finally:
        with maintenance.connect() as db:
            active = (
                db.execute(
                    text(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE datname=:database AND pid <> pg_backend_pid()"
                    ),
                    {"database": database_name},
                )
                .scalars()
                .all()
            )
            assert active == []
            db.execute(text(f'DROP DATABASE "{database_name}"'))
        maintenance.dispose()


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


def test_fresh_upgrade_installs_independent_heads_and_deterministic_seed(
    fresh_configuration_database_url: URL,
) -> None:
    config = _config(fresh_configuration_database_url)
    command.upgrade(config, "heads")
    engine = create_engine(fresh_configuration_database_url)
    try:
        expected_heads = set(ScriptDirectory.from_config(config).get_heads())
        with engine.connect() as db:
            installed_heads = set(
                db.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
            counts = db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM identity.policy_key), "
                    "(SELECT count(*) FROM identity.version), "
                    "(SELECT count(*) FROM identity.active_pointer), "
                    "(SELECT count(*) FROM audit.audit_event "
                    " WHERE id='configuration-system-seed-identity-v1')"
                )
            ).one()
        assert expected_heads == {
            "0008_audit_requirement_grant",
            "0010_identity_policy_reauth",
            "0001_organization_base",
            "0001_workspace_base",
            "0007_auth_v04_routes",
            "0005_req_sdd_human_gate",
            "0006_sc_mr_reconcile",
        }
        assert installed_heads == {
            "0008_audit_requirement_grant",
            "0010_identity_policy_reauth",
            "0007_auth_v04_routes",
            "0005_req_sdd_human_gate",
            "0006_sc_mr_reconcile",
        }
        assert counts == (7, 1, 1, 1)
    finally:
        engine.dispose()


def test_configuration_upgrade_preserves_preexisting_identity_and_audit_facts(
    fresh_configuration_database_url: URL,
) -> None:
    config = _config(fresh_configuration_database_url)
    command.upgrade(config, "0002_audit_transactional_append")
    command.upgrade(config, "0004_identity_bootstrap_totp_cap")
    engine = create_engine(fresh_configuration_database_url)
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO identity.account "
                    "(id, employee_no, display_name, status) VALUES "
                    "('00000000-0000-0000-0000-000000001101', "
                    "'00001101', 'Existing account', 'PENDING_INIT')"
                )
            )
            db.execute(
                text(
                    "INSERT INTO audit.audit_event "
                    "(id, occurred_at, actor, actor_type, action, target_type, target_id, "
                    "result, correlation_id, schema_version) VALUES "
                    "('00000000-0000-0000-0000-000000001102', now(), 'SYSTEM', 'system', "
                    "'existing.event', 'account', 'existing', 'SUCCESS', "
                    "'task11-preexisting', 1)"
                )
            )
        command.upgrade(config, "heads")
        with engine.connect() as db:
            preserved = db.execute(
                text(
                    "SELECT employee_no, display_name FROM identity.account "
                    "WHERE id='00000000-0000-0000-0000-000000001101'"
                )
            ).one()
            event_count = db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE id='00000000-0000-0000-0000-000000001102'"
                )
            ).scalar_one()
        assert preserved == ("00001101", "Existing account")
        assert event_count == 1
    finally:
        engine.dispose()


def test_all_migrations_downgrade_and_fresh_reupgrade_cleanly(
    fresh_configuration_database_url: URL,
) -> None:
    config = _config(fresh_configuration_database_url)
    command.upgrade(config, "heads")
    command.downgrade(config, "base")
    engine = create_engine(fresh_configuration_database_url)
    try:
        assert "identity" not in inspect(engine).get_schema_names()
        assert "audit" not in inspect(engine).get_schema_names()
        command.upgrade(config, "heads")
        assert {
            "policy_key",
            "draft",
            "version",
            "active_pointer",
            "configuration_idempotency_record",
            "configuration_outbox",
        } <= set(inspect(engine).get_table_names(schema="identity"))
    finally:
        engine.dispose()


def test_identity_0010_downgrade_restores_0009_configuration_publish_privileges(
    fresh_configuration_database_url: URL,
) -> None:
    config = _config(fresh_configuration_database_url)
    command.upgrade(config, "0010_identity_policy_reauth")
    engine = create_engine(fresh_configuration_database_url)
    try:

        def configuration_privileges() -> tuple[bool, bool, bool]:
            with engine.connect() as db:
                values = db.execute(
                    text(
                        "SELECT "
                        "has_table_privilege('configuration_rw', 'identity.version', "
                        "'INSERT'), "
                        "has_table_privilege('configuration_rw', "
                        "'identity.active_pointer', 'UPDATE'), "
                        "has_table_privilege('configuration_rw', "
                        "'identity.configuration_outbox', 'SELECT')"
                    )
                ).one()
                return bool(values[0]), bool(values[1]), bool(values[2])

        assert configuration_privileges() == (False, False, False)
        command.downgrade(config, "0009_identity_policy_publish")
        assert configuration_privileges() == (True, True, True)
        command.upgrade(config, "0010_identity_policy_reauth")
        assert configuration_privileges() == (False, False, False)
    finally:
        engine.dispose()
