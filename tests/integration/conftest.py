import pytest
from sqlalchemy import Engine, create_engine, text

from control_plane.app.shared.db.settings import DbSettings


def _engine_or_skip(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL 不可用：先 docker compose up -d 并 uv run alembic upgrade head")
    return engine


@pytest.fixture(scope="session")
def owner_engine() -> Engine:
    return _engine_or_skip(DbSettings().migration_database_url)


@pytest.fixture(scope="session")
def rw_engine() -> Engine:
    return _engine_or_skip(DbSettings().database_url)
