import os
from collections.abc import Iterator
from contextlib import contextmanager
from runpy import run_path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url

from control_plane.app.shared.db.settings import DbSettings


def _required_engine(url: str, *, role: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as db:
            actual_role = db.execute(text("SELECT current_user")).scalar_one()
            server_version = db.execute(text("SHOW server_version_num")).scalar_one()
    except Exception:
        engine.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            pytest.fail(f"Required PostgreSQL integration database unavailable for {role}")
        pytest.skip(f"PostgreSQL integration database unavailable for {role}")
    assert actual_role == role
    assert int(server_version) >= 180000
    return engine


@pytest.fixture(scope="session")
def configuration_owner_engine() -> Iterator[Engine]:
    engine = _required_engine(DbSettings().migration_database_url, role="platform_owner")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def configuration_seed(configuration_owner_engine: Engine) -> None:
    seed_sql = str(run_path("migrations/identity/0005_configuration_policy.py")["_SEED_SQL"])
    with configuration_owner_engine.begin() as db:
        db.exec_driver_sql(seed_sql)


@contextmanager
def _temporary_runtime_role_engine(
    owner_engine: Engine,
    runtime_url: str,
    *,
    privilege_role: str,
) -> Iterator[tuple[Engine, str]]:
    login_role = f"test_{privilege_role}_login_{uuid4().hex}"
    quoted_login_role = f'"{login_role}"'
    test_password = f"test-only-{uuid4().hex}"
    engine = create_engine(
        make_url(runtime_url).set(username=login_role, password=test_password),
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "checkout")
    def _assume_privilege_role(dbapi_connection: object, *_args: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute(f"SET ROLE {privilege_role}")
        finally:
            cursor.close()

    role_created = False
    try:
        with owner_engine.begin() as db:
            db.execute(text(f"CREATE ROLE {quoted_login_role} LOGIN PASSWORD '{test_password}'"))
            role_created = True
            db.execute(text(f"GRANT {privilege_role} TO {quoted_login_role}"))
        with engine.connect() as db:
            current_role, session_role = db.execute(text("SELECT current_user, session_user")).one()
        assert current_role == privilege_role
        assert session_role == login_role
        yield engine, login_role
    finally:
        engine.dispose()
        if role_created:
            with owner_engine.begin() as db:
                db.execute(text(f"REVOKE {privilege_role} FROM {quoted_login_role}"))
                db.execute(text(f"DROP ROLE {quoted_login_role}"))


@pytest.fixture(scope="session")
def configuration_rw_engine(configuration_owner_engine: Engine) -> Iterator[Engine]:
    runtime_url = os.environ.get(
        "CONFIGURATION_DATABASE_URL",
        "postgresql+psycopg://configuration_rw:localdev@localhost:5432/platform",
    )
    with _temporary_runtime_role_engine(
        configuration_owner_engine,
        runtime_url,
        privilege_role="configuration_rw",
    ) as runtime:
        yield runtime[0]


@pytest.fixture(scope="session")
def identity_rw_engine(configuration_owner_engine: Engine) -> Iterator[Engine]:
    runtime_url = os.environ.get(
        "IDENTITY_DATABASE_URL",
        "postgresql+psycopg://identity_rw:localdev@localhost:5432/platform",
    )
    with _temporary_runtime_role_engine(
        configuration_owner_engine,
        runtime_url,
        privilege_role="identity_rw",
    ) as runtime:
        yield runtime[0]
