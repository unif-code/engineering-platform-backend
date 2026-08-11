import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from control_plane.app.shared.db.settings import DbSettings


def _required_engine(url: str, *, role: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            actual_role = conn.execute(text("SELECT current_user")).scalar_one()
    except Exception:
        engine.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            pytest.fail(f"Required PostgreSQL integration database unavailable for {role}")
        pytest.skip(f"PostgreSQL integration database unavailable for {role}")
    assert actual_role == role
    return engine


@pytest.fixture(scope="session")
def organization_owner_engine() -> Engine:
    return _required_engine(DbSettings().migration_database_url, role="platform_owner")


@pytest.fixture(scope="session")
def organization_rw_engine() -> Engine:
    url = os.environ.get(
        "ORGANIZATION_DATABASE_URL",
        "postgresql+psycopg://organization_rw:localdev@localhost:5432/platform",
    )
    return _required_engine(url, role="organization_rw")


@pytest.fixture(scope="session")
def organization_identity_engine() -> Engine:
    return _required_engine(DbSettings().identity_database_url, role="identity_rw")


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
