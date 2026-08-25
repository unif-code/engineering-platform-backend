import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_requirement_database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    owner_url = make_url(os.environ["MIGRATION_DATABASE_URL"])
    database_name = f"test_requirement_migration_{uuid4().hex}"
    maintenance = create_engine(
        owner_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with maintenance.connect() as db:
        db.execute(text(f'CREATE DATABASE "{database_name}"'))
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


def test_fresh_upgrade_installs_requirement_and_all_visible_heads(
    fresh_requirement_database_url: str,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "heads")
    engine = create_engine(fresh_requirement_database_url)
    try:
        expected_heads = set(ScriptDirectory.from_config(config).get_heads())
        with engine.connect() as db:
            installed_heads = set(
                db.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
        assert expected_heads == {
            "0008_audit_requirement_grant",
            "0010_identity_policy_reauth",
            "0001_organization_base",
            "0001_workspace_base",
            "0006_auth_v03_routes",
            "0001_requirement_base",
        }
        assert installed_heads == {
            "0008_audit_requirement_grant",
            "0010_identity_policy_reauth",
            "0006_auth_v03_routes",
        }
        assert set(inspect(engine).get_table_names(schema="requirement")) == {
            "decision",
            "gate_assignment",
            "gate_instance",
            "idempotency_record",
            "outbox_message",
            "requirement",
            "sdd_baseline",
            "work_item",
        }
    finally:
        engine.dispose()


def test_all_migrations_round_trip_with_requirement_schema(
    fresh_requirement_database_url: str,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "heads")
    command.downgrade(config, "base")
    engine = create_engine(fresh_requirement_database_url)
    try:
        assert "requirement" not in inspect(engine).get_schema_names()
        command.upgrade(config, "heads")
        assert set(inspect(engine).get_table_names(schema="requirement")) == {
            "decision",
            "gate_assignment",
            "gate_instance",
            "idempotency_record",
            "outbox_message",
            "requirement",
            "sdd_baseline",
            "work_item",
        }
    finally:
        engine.dispose()
