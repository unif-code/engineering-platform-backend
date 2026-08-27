from collections.abc import Iterator
from contextlib import contextmanager
from runpy import run_path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL

from control_plane.app.shared.db.settings import DbSettings
from tests.integration_database import parse_database_url
from tests.integration_database import required_engine as _required_engine


@pytest.fixture(scope="session")
def configuration_owner_engine() -> Iterator[Engine]:
    engine = _required_engine(
        parse_database_url(
            DbSettings().migration_database_url,
            setting_name="MIGRATION_DATABASE_URL",
        ),
        role="platform_owner",
        minimum_server_version=180000,
    )
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
    runtime_url: URL,
    *,
    privilege_role: str,
) -> Iterator[tuple[Engine, str]]:
    login_role = f"test_{privilege_role}_login_{uuid4().hex}"
    quoted_login_role = f'"{login_role}"'
    test_password = f"test-only-{uuid4().hex}"
    engine = create_engine(
        runtime_url.set(username=login_role, password=test_password),
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
    runtime_url = parse_database_url(
        DbSettings().configuration_database_url,
        setting_name="CONFIGURATION_DATABASE_URL",
    )
    with _temporary_runtime_role_engine(
        configuration_owner_engine,
        runtime_url,
        privilege_role="configuration_rw",
    ) as runtime:
        yield runtime[0]


@pytest.fixture(scope="session")
def identity_rw_engine(configuration_owner_engine: Engine) -> Iterator[Engine]:
    runtime_url = parse_database_url(
        DbSettings().identity_database_url,
        setting_name="IDENTITY_DATABASE_URL",
    )
    with _temporary_runtime_role_engine(
        configuration_owner_engine,
        runtime_url,
        privilege_role="identity_rw",
    ) as runtime:
        yield runtime[0]
