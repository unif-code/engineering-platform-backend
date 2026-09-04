from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

from control_plane.app.shared.db.settings import DbSettings
from tests.configuration.conftest import _temporary_runtime_role_engine
from tests.integration_database import parse_database_url
from tests.integration_database import required_engine as _required_engine


@pytest.fixture(scope="session")
def identity_owner_engine() -> Engine:
    return _required_engine(
        parse_database_url(
            DbSettings().migration_database_url,
            setting_name="MIGRATION_DATABASE_URL",
        ),
        role="platform_owner",
    )


@pytest.fixture(scope="session")
def identity_rw_engine() -> Engine:
    return _required_engine(
        parse_database_url(
            DbSettings().identity_database_url,
            setting_name="IDENTITY_DATABASE_URL",
        ),
        role="identity_rw",
    )


@pytest.fixture
def clean_identity_db(identity_owner_engine: Engine) -> Iterator[None]:
    with identity_owner_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE identity.idempotency_record, identity.auth_challenge, "
                "identity.session, identity.temp_credential, identity.login_backoff, "
                "identity.account, audit.audit_event"
            )
        )

    yield
    with identity_owner_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE identity.idempotency_record, identity.auth_challenge, "
                "identity.session, identity.temp_credential, identity.login_backoff, "
                "identity.account, audit.audit_event"
            )
        )


@dataclass(frozen=True, slots=True)
class IsolatedIdentityDatabase:
    owner: Engine
    runtime: Engine


@pytest.fixture
def isolated_identity_database(
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[IsolatedIdentityDatabase]:
    """Disposable DB and disposable login; never change the development database."""
    name = f"test_identity_reauth_{uuid4().hex}"
    maintenance = create_engine(
        identity_owner_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with maintenance.connect() as db:
        db.execute(text(f'CREATE DATABASE "{name}"'))
    target_url = identity_owner_engine.url.set(database=name)
    owner = create_engine(target_url, pool_pre_ping=True)
    try:
        monkeypatch.setenv(
            "MIGRATION_DATABASE_URL", target_url.render_as_string(hide_password=False)
        )
        command.upgrade(Config("alembic.ini"), "heads")
        with _temporary_runtime_role_engine(
            owner, target_url, privilege_role="identity_rw"
        ) as runtime:
            yield IsolatedIdentityDatabase(owner=owner, runtime=runtime[0])
    finally:
        owner.dispose()
        with maintenance.connect() as db:
            db.execute(text(f'DROP DATABASE "{name}"'))
        maintenance.dispose()
