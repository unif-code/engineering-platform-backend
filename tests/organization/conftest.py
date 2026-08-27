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
def organization_owner_engine() -> Engine:
    return _required_engine(
        parse_database_url(
            DbSettings().migration_database_url,
            setting_name="MIGRATION_DATABASE_URL",
        ),
        role="platform_owner",
    )


@contextmanager
def _temporary_organization_role_engine(
    organization_owner_engine: Engine,
    runtime_url: URL,
) -> Iterator[tuple[Engine, str]]:
    login_role = f"test_organization_login_{uuid4().hex}"
    quoted_login_role = f'"{login_role}"'
    test_password = "test-only-organization-password"
    engine = create_engine(
        runtime_url.set(username=login_role, password=test_password),
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "checkout")
    def _assume_privilege_role(dbapi_connection: object, *_args: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("SET ROLE organization_rw")
        finally:
            cursor.close()

    try:
        with organization_owner_engine.begin() as db:
            db.execute(text(f"CREATE ROLE {quoted_login_role} LOGIN PASSWORD '{test_password}'"))
            db.execute(text(f"GRANT organization_rw TO {quoted_login_role}"))
        with engine.connect() as conn:
            current_role, session_role = conn.execute(
                text("SELECT current_user, session_user")
            ).one()
        assert current_role == "organization_rw"
        assert session_role == login_role
        yield engine, login_role
    finally:
        engine.dispose()
        with organization_owner_engine.begin() as db:
            role_exists = db.execute(
                text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname=:role_name)"),
                {"role_name": login_role},
            ).scalar_one()
            if role_exists:
                db.execute(text(f"REVOKE organization_rw FROM {quoted_login_role}"))
                db.execute(text(f"DROP ROLE {quoted_login_role}"))


@pytest.fixture(scope="session")
def organization_rw_engine(organization_owner_engine: Engine) -> Iterator[Engine]:
    url = parse_database_url(
        DbSettings().organization_database_url,
        setting_name="ORGANIZATION_DATABASE_URL",
    )
    with _temporary_organization_role_engine(organization_owner_engine, url) as runtime:
        yield runtime[0]


@pytest.fixture(scope="session")
def organization_identity_engine() -> Engine:
    return _required_engine(
        parse_database_url(
            DbSettings().identity_database_url,
            setting_name="IDENTITY_DATABASE_URL",
        ),
        role="identity_rw",
    )


@pytest.fixture
def clean_organization_db(organization_owner_engine: Engine) -> Iterator[None]:
    with organization_owner_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE organization.idempotency_record, organization.org_edge, "
                "identity.idempotency_record, identity.auth_challenge, identity.session, "
                "identity.temp_credential, identity.login_backoff, identity.account, "
                "audit.audit_event"
            )
        )
    yield
    with organization_owner_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE organization.idempotency_record, organization.org_edge, "
                "identity.idempotency_record, identity.auth_challenge, identity.session, "
                "identity.temp_credential, identity.login_backoff, identity.account, "
                "audit.audit_event"
            )
        )
