import pytest
from sqlalchemy import Engine

from control_plane.app.shared.db.settings import DbSettings
from tests.integration_database import engine_or_skip as _engine_or_skip
from tests.integration_database import parse_database_url


@pytest.fixture(scope="session")
def owner_engine() -> Engine:
    return _engine_or_skip(
        parse_database_url(
            DbSettings().migration_database_url,
            setting_name="MIGRATION_DATABASE_URL",
        )
    )


@pytest.fixture(scope="session")
def rw_engine() -> Engine:
    return _engine_or_skip(
        parse_database_url(
            DbSettings().database_url,
            setting_name="DATABASE_URL",
        )
    )
