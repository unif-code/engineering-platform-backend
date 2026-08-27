from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL

from control_plane.app.shared.db.settings import DbSettings
from tests.integration_database import parse_database_url
from tests.integration_database import required_engine as _required_engine
from tests.organization.conftest import _temporary_organization_role_engine


@pytest.fixture(scope="session")
def workspace_owner_engine() -> Iterator[Engine]:
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
def _temporary_workspace_role_engine(
    workspace_owner_engine: Engine,
    runtime_url: URL,
) -> Iterator[tuple[Engine, str]]:
    login_role = f"test_workspace_login_{uuid4().hex}"
    quoted_login_role = f'"{login_role}"'
    test_password = "test-only-workspace-password"
    engine = create_engine(
        runtime_url.set(username=login_role, password=test_password),
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "checkout")
    def _assume_privilege_role(dbapi_connection: object, *_args: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("SET ROLE workspace_rw")
        finally:
            cursor.close()

    try:
        with workspace_owner_engine.begin() as db:
            db.execute(text(f"CREATE ROLE {quoted_login_role} LOGIN PASSWORD '{test_password}'"))
            db.execute(text(f"GRANT workspace_rw TO {quoted_login_role}"))
        with engine.connect() as conn:
            current_role, session_role = conn.execute(
                text("SELECT current_user, session_user")
            ).one()
        assert current_role == "workspace_rw"
        assert session_role == login_role
        yield engine, login_role
    finally:
        engine.dispose()
        with workspace_owner_engine.begin() as db:
            role_exists = db.execute(
                text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname=:role_name)"),
                {"role_name": login_role},
            ).scalar_one()
            if role_exists:
                db.execute(text(f"REVOKE workspace_rw FROM {quoted_login_role}"))
                db.execute(text(f"DROP ROLE {quoted_login_role}"))


@pytest.fixture(scope="session")
def workspace_rw_engine(workspace_owner_engine: Engine) -> Iterator[Engine]:
    with _temporary_workspace_role_engine(
        workspace_owner_engine,
        parse_database_url(
            DbSettings().workspace_database_url,
            setting_name="WORKSPACE_DATABASE_URL",
        ),
    ) as runtime:
        yield runtime[0]


@pytest.fixture(scope="session")
def workspace_identity_engine() -> Iterator[Engine]:
    engine = _required_engine(
        parse_database_url(
            DbSettings().identity_database_url,
            setting_name="IDENTITY_DATABASE_URL",
        ),
        role="identity_rw",
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def workspace_organization_engine(
    workspace_owner_engine: Engine,
) -> Iterator[Engine]:
    with _temporary_organization_role_engine(
        workspace_owner_engine,
        parse_database_url(
            DbSettings().organization_database_url,
            setting_name="ORGANIZATION_DATABASE_URL",
        ),
    ) as runtime:
        yield runtime[0]


@pytest.fixture
def clean_workspace_db(workspace_owner_engine: Engine) -> Iterator[None]:
    tables = (
        "workspace.members_projection, workspace.leader, workspace.idempotency_record, "
        "workspace.workspace, organization.idempotency_record, organization.org_edge, "
        "identity.idempotency_record, identity.auth_challenge, identity.session, "
        "identity.temp_credential, identity.login_backoff, identity.account, "
        "audit.audit_event"
    )
    with workspace_owner_engine.begin() as db:
        db.execute(text(f"TRUNCATE {tables}"))
    yield
    with workspace_owner_engine.begin() as db:
        db.execute(text(f"TRUNCATE {tables}"))
