import pytest
from sqlalchemy import Engine, create_engine, text

from control_plane.app.shared.db.settings import DbSettings


def _required_engine(url: str, *, role: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            actual_role = conn.execute(text("SELECT current_user")).scalar_one()
    except Exception as exc:
        pytest.fail(f"Task 4 PostgreSQL unavailable for {role}: {exc!r}")
    assert actual_role == role
    return engine


@pytest.fixture(scope="session")
def identity_owner_engine() -> Engine:
    return _required_engine(DbSettings().migration_database_url, role="platform_owner")


@pytest.fixture(scope="session")
def identity_rw_engine() -> Engine:
    return _required_engine(DbSettings().identity_database_url, role="identity_rw")
