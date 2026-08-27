from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from control_plane.app.shared.db.settings import DbSettings
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
