import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url

from control_plane.app.shared.db.settings import DbSettings


def _required_engine(url: str, *, role: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as db:
            actual_role = db.execute(text("SELECT current_user")).scalar_one()
    except Exception:
        engine.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            pytest.fail(f"Required PostgreSQL integration database unavailable for {role}")
        pytest.skip(f"PostgreSQL integration database unavailable for {role}")
    assert actual_role == role
    return engine


@pytest.fixture(scope="session")
def requirement_owner_engine() -> Iterator[Engine]:
    engine = _required_engine(DbSettings().migration_database_url, role="platform_owner")
    yield engine
    engine.dispose()


@contextmanager
def _temporary_requirement_role_engine(
    requirement_owner_engine: Engine,
) -> Iterator[Engine]:
    login_role = f"test_requirement_login_{uuid4().hex}"
    quoted_login_role = f'"{login_role}"'
    test_password = "test-only-requirement-password"
    owner_url = make_url(DbSettings().migration_database_url)
    runtime_url = owner_url.set(username=login_role, password=test_password)
    engine = create_engine(runtime_url, pool_pre_ping=True)

    @event.listens_for(engine, "checkout")
    def _assume_privilege_role(dbapi_connection: object, *_args: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("SET ROLE requirement_rw")
        finally:
            cursor.close()

    try:
        with requirement_owner_engine.begin() as db:
            db.execute(text(f"CREATE ROLE {quoted_login_role} LOGIN PASSWORD '{test_password}'"))
            db.execute(text(f"GRANT requirement_rw TO {quoted_login_role}"))
        with engine.connect() as db:
            current_role, session_role = db.execute(text("SELECT current_user, session_user")).one()
        assert current_role == "requirement_rw"
        assert session_role == login_role
        yield engine
    finally:
        engine.dispose()
        with requirement_owner_engine.begin() as db:
            role_exists = db.execute(
                text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname=:role_name)"),
                {"role_name": login_role},
            ).scalar_one()
            if role_exists:
                db.execute(text(f"REVOKE requirement_rw FROM {quoted_login_role}"))
                db.execute(text(f"DROP ROLE {quoted_login_role}"))


@pytest.fixture(scope="session")
def requirement_rw_engine(requirement_owner_engine: Engine) -> Iterator[Engine]:
    with _temporary_requirement_role_engine(requirement_owner_engine) as engine:
        yield engine


@pytest.fixture
def isolated_requirement_database(
    requirement_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator["IsolatedRequirementDatabase"]:
    owner_url = make_url(DbSettings().migration_database_url)
    database_name = f"test_requirement_repository_{uuid4().hex}"
    maintenance = create_engine(
        owner_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with maintenance.connect() as db:
        db.execute(text(f'CREATE DATABASE "{database_name}"'))
    target_url = owner_url.set(database=database_name).render_as_string(hide_password=False)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", target_url)
    command.upgrade(Config("alembic.ini"), "heads")
    isolated_owner = create_engine(target_url, pool_pre_ping=True)
    try:
        with _temporary_requirement_role_engine(isolated_owner) as engine:
            yield IsolatedRequirementDatabase(owner=isolated_owner, runtime=engine)
    finally:
        isolated_owner.dispose()
        with maintenance.connect() as db:
            db.execute(text(f'DROP DATABASE "{database_name}"'))
        maintenance.dispose()


@dataclass(frozen=True, slots=True)
class IsolatedRequirementDatabase:
    owner: Engine
    runtime: Engine


@pytest.fixture
def isolated_requirement_rw_engine(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> Engine:
    return isolated_requirement_database.runtime
