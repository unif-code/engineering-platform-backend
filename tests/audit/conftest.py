from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL

from control_plane.app.shared.db.settings import DbSettings
from tests.integration_database import parse_database_url
from tests.integration_database import required_engine as _required_engine


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
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
def _temporary_runtime_role_engine(
    owner: Engine,
    *,
    privilege_role: str,
    runtime_url: URL,
) -> Iterator[Engine]:
    login_role = f"test_{privilege_role}_login_{uuid4().hex}"
    quoted_login_role = f'"{login_role}"'
    test_password = "test-only-audit-password"
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
        with owner.begin() as db:
            db.execute(text(f"CREATE ROLE {quoted_login_role} LOGIN PASSWORD '{test_password}'"))
            role_created = True
            db.execute(text(f"GRANT {privilege_role} TO {quoted_login_role}"))
        with engine.connect() as db:
            current_role, session_role = db.execute(text("SELECT current_user, session_user")).one()
        assert current_role == privilege_role
        assert session_role == login_role
        yield engine
    finally:
        engine.dispose()
        if role_created:
            with owner.begin() as db:
                role_exists = db.execute(
                    text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname=:role_name)"),
                    {"role_name": login_role},
                ).scalar_one()
                if role_exists:
                    db.execute(text(f"REVOKE {privilege_role} FROM {quoted_login_role}"))
                    db.execute(text(f"DROP ROLE {quoted_login_role}"))


@pytest.fixture(scope="session")
def rw_engine(owner_engine: Engine) -> Iterator[Engine]:
    with _temporary_runtime_role_engine(
        owner_engine,
        privilege_role="audit_rw",
        runtime_url=parse_database_url(
            DbSettings().database_url,
            setting_name="DATABASE_URL",
        ),
    ) as engine:
        yield engine


@pytest.fixture(scope="session")
def identity_rw_engine(owner_engine: Engine) -> Iterator[Engine]:
    with _temporary_runtime_role_engine(
        owner_engine,
        privilege_role="identity_rw",
        runtime_url=parse_database_url(
            DbSettings().identity_database_url,
            setting_name="IDENTITY_DATABASE_URL",
        ),
    ) as engine:
        yield engine


@pytest.fixture(scope="session")
def authorization_rw_engine(owner_engine: Engine) -> Iterator[Engine]:
    with _temporary_runtime_role_engine(
        owner_engine,
        privilege_role="authorization_rw",
        runtime_url=parse_database_url(
            DbSettings().authorization_database_url,
            setting_name="AUTHORIZATION_DATABASE_URL",
        ),
    ) as engine:
        yield engine


@pytest.fixture(autouse=True)
def clean_audit_events(owner_engine: Engine) -> Iterator[None]:
    with owner_engine.begin() as db:
        db.execute(
            text(
                'TRUNCATE "authorization".convergence_principal_pending, '
                '"authorization".convergence_work, "authorization".idempotency_record, '
                '"authorization"."grant", "authorization".principal_version, '
                "identity.idempotency_record, identity.auth_challenge, identity.session, "
                "identity.temp_credential, identity.login_backoff, identity.account, "
                "audit.audit_event"
            )
        )
    yield
    with owner_engine.begin() as db:
        db.execute(
            text(
                'TRUNCATE "authorization".convergence_principal_pending, '
                '"authorization".convergence_work, "authorization".idempotency_record, '
                '"authorization"."grant", "authorization".principal_version, '
                "identity.idempotency_record, identity.auth_challenge, identity.session, "
                "identity.temp_credential, identity.login_backoff, identity.account, "
                "audit.audit_event"
            )
        )
