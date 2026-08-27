import os

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url


def parse_database_url(value: str, *, setting_name: str) -> URL:
    """Parse one configured URL without exposing its value in pytest tracebacks."""
    __tracebackhide__ = True
    parsed: URL | None = None
    try:
        parsed = make_url(value)
    except Exception:
        pass
    if parsed is None:
        pytest.fail(
            f"{setting_name} must be a valid SQLAlchemy database URL",
            pytrace=False,
        )
    return parsed


def migration_database_url() -> URL:
    """Return the explicitly configured migration URL or a value-free test outcome."""
    __tracebackhide__ = True
    configured = os.environ.get("MIGRATION_DATABASE_URL")
    if configured is None:
        if os.environ.get("REQUIRE_INTEGRATION_DB") == "1":
            pytest.fail(
                "MIGRATION_DATABASE_URL is required for PostgreSQL integration tests",
                pytrace=False,
            )
        pytest.skip(
            "MIGRATION_DATABASE_URL is not configured; PostgreSQL integration tests skipped"
        )
    return parse_database_url(configured, setting_name="MIGRATION_DATABASE_URL")


def required_engine(
    url: URL,
    *,
    role: str,
    minimum_server_version: int | None = None,
) -> Engine:
    """Connect to a required integration database without leaking its URL."""
    __tracebackhide__ = True
    if not isinstance(url, URL):
        pytest.fail("Integration database URL must be parsed before use", pytrace=False)

    engine: Engine | None = None
    actual_role: str | None = None
    server_version: int | None = None
    unavailable = False
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as db:
            actual_role = db.execute(text("SELECT current_user")).scalar_one()
            if minimum_server_version is not None:
                server_version = int(db.execute(text("SHOW server_version_num")).scalar_one())
    except Exception:
        if engine is not None:
            engine.dispose()
        unavailable = True

    if unavailable:
        if os.environ.get("REQUIRE_INTEGRATION_DB") == "1":
            pytest.fail(
                f"Required PostgreSQL integration database unavailable for {role}",
                pytrace=False,
            )
        pytest.skip(f"PostgreSQL integration database unavailable for {role}")

    if engine is None or actual_role != role:
        if engine is not None:
            engine.dispose()
        pytest.fail(f"PostgreSQL integration database role mismatch for {role}", pytrace=False)
    if minimum_server_version is not None and (
        server_version is None or server_version < minimum_server_version
    ):
        engine.dispose()
        pytest.fail("PostgreSQL integration database server version is unsupported", pytrace=False)
    return engine


def engine_or_skip(url: URL) -> Engine:
    """Connect for optional integration tests without leaking its URL."""
    __tracebackhide__ = True
    if not isinstance(url, URL):
        pytest.fail("Integration database URL must be parsed before use", pytrace=False)

    engine: Engine | None = None
    unavailable = False
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        if engine is not None:
            engine.dispose()
        unavailable = True

    if unavailable:
        pytest.skip("PostgreSQL unavailable; start the local integration database first")
    if engine is None:
        pytest.fail("PostgreSQL integration database engine was not created", pytrace=False)
    return engine
