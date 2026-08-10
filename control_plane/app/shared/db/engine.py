from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from control_plane.app.shared.db.settings import DbSettings


@lru_cache(maxsize=1)
def runtime_engine() -> Engine:
    return create_engine(DbSettings().database_url, pool_pre_ping=True)


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
