from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text

from control_plane.app.shared.db.settings import DbSettings
from control_plane.app.shared.security import SecretMaterial
from tests.integration_database import parse_database_url
from tests.integration_database import required_engine as _required_engine


class TestSecretManager:
    def load(self) -> SecretMaterial:
        return SecretMaterial(
            password_pepper=b"p" * 32, totp_sealing_key=b"t" * 32, idempotency_sealing_key=b"i" * 32
        )


@pytest.fixture(scope="session")
def agent_owner_engine() -> Iterator[Engine]:
    engine = _required_engine(
        parse_database_url(
            DbSettings().migration_database_url,
            setting_name="MIGRATION_DATABASE_URL",
        ),
        role="platform_owner",
    )
    yield engine
    engine.dispose()


@contextmanager
def _temporary_agent_role_engine(owner_engine: Engine) -> Iterator[Engine]:
    login_role = f"test_agent_login_{uuid4().hex}"
    quoted_login_role = f'"{login_role}"'
    test_password = "test-only-agent-password"
    runtime_engine = create_engine(
        owner_engine.url.set(username=login_role, password=test_password),
        pool_pre_ping=True,
    )

    @event.listens_for(runtime_engine, "checkout")
    def _assume_agent_role(dbapi_connection: object, *_args: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("SET ROLE agent_rw")
        finally:
            cursor.close()

    try:
        with owner_engine.begin() as db:
            db.execute(text(f"CREATE ROLE {quoted_login_role} LOGIN PASSWORD '{test_password}'"))
            db.execute(text(f"GRANT agent_rw TO {quoted_login_role}"))
        with runtime_engine.connect() as db:
            assert db.execute(text("SELECT current_user")).scalar_one() == "agent_rw"
        yield runtime_engine
    finally:
        runtime_engine.dispose()
        with owner_engine.begin() as db:
            exists = db.execute(
                text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname=:role_name)"),
                {"role_name": login_role},
            ).scalar_one()
            if exists:
                db.execute(text(f"REVOKE agent_rw FROM {quoted_login_role}"))
                db.execute(text(f"DROP ROLE {quoted_login_role}"))


@dataclass(frozen=True, slots=True)
class IsolatedAgentDatabase:
    owner: Engine
    runtime: Engine


@pytest.fixture
def isolated_agent_database(
    agent_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[IsolatedAgentDatabase]:
    database_name = f"test_agent_{uuid4().hex}"
    maintenance = create_engine(
        agent_owner_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with maintenance.connect() as db:
        db.execute(text(f'CREATE DATABASE "{database_name}"'))
    target_url = agent_owner_engine.url.set(database=database_name)
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        target_url.render_as_string(hide_password=False),
    )
    command.upgrade(Config("alembic.ini"), "heads")
    isolated_owner = create_engine(target_url, pool_pre_ping=True)
    try:
        with _temporary_agent_role_engine(isolated_owner) as runtime:
            yield IsolatedAgentDatabase(owner=isolated_owner, runtime=runtime)
    finally:
        isolated_owner.dispose()
        with maintenance.connect() as db:
            db.execute(text(f'DROP DATABASE "{database_name}"'))
        maintenance.dispose()
