"""PostgreSQL owner upgrade/rollback and worker-role fence proof; CI only."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from control_plane.app.modules.model_gateway.adapters import SqlAlchemyDeploymentRepository
from control_plane.app.modules.model_gateway.adapters.checks import SqlAlchemyCheckRepository
from tests.model_gateway.dispatch_support import check_dispatch
from tests.source_control.test_migration import _config
from tests.source_control.test_migration import (
    fresh_source_control_migration_database_url as fresh_source_control_migration_database_url,
)

pytestmark = pytest.mark.integration
OLD = "0008_model_metadata_observations"
NEW = "0009_model_check_deadline"
CLAIM = text("""
    UPDATE model_gateway.connection_check
    SET state='RUNNING',revision=revision+1,attempt=1,
        execution_token=:token,started_at=:started,deadline_at=:deadline
    WHERE id=:id
""")


def seed(engine: Engine, tmp_path: Path) -> tuple[str, datetime]:
    h = check_dispatch(tmp_path)
    with engine.begin() as db:
        assert SqlAlchemyDeploymentRepository(db).insert(h.store.candidate) is not None
        assert SqlAlchemyCheckRepository(db).insert_check(h.store.checks[h.identifier])
    return str(h.identifier), h.now


def stored_row(engine: Engine, identifier: str) -> str:
    with engine.connect() as db:
        return str(
            db.execute(
                text("SELECT to_jsonb(c)::text FROM model_gateway.connection_check c WHERE id=:id"),
                {"id": identifier},
            ).scalar_one()
        )


def installed_heads(engine: Engine) -> set[str]:
    with engine.connect() as db:
        return set(db.execute(text("SELECT version_num FROM alembic_version")).scalars())


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "UNKNOWN"])
def test_upgrade_and_downgrade_preserve_valid_historical_rows(
    fresh_source_control_migration_database_url: URL, tmp_path: Path, state: str
) -> None:
    config = _config(fresh_source_control_migration_database_url)
    command.upgrade(config, f"model_gateway@{OLD}")
    engine = create_engine(fresh_source_control_migration_database_url)
    try:
        identifier, now = seed(engine, tmp_path)
        if state != "QUEUED":
            with engine.begin() as db:
                db.execute(text("SET LOCAL ROLE model_gateway_worker_rw"))
                db.execute(
                    CLAIM,
                    {
                        "id": identifier,
                        "token": str(uuid4()),
                        "started": now,
                        "deadline": now + timedelta(seconds=25),
                    },
                )
                if state == "UNKNOWN":
                    db.execute(
                        text(
                            "UPDATE model_gateway.connection_check SET state='UNKNOWN', "
                            "revision=revision+1,reason='EXECUTION_EXPIRED',finished_at=:finished "
                            "WHERE id=:id"
                        ),
                        {"id": identifier, "finished": now + timedelta(seconds=26)},
                    )
        original = stored_row(engine, identifier)
        command.upgrade(config, f"model_gateway@{NEW}")
        assert installed_heads(engine) == {NEW}
        assert stored_row(engine, identifier) == original
        command.downgrade(config, f"model_gateway@{OLD}")
        assert installed_heads(engine) == {OLD}
        assert stored_row(engine, identifier) == original
        command.upgrade(config, f"model_gateway@{NEW}")
        assert installed_heads(engine) == {NEW}
        assert stored_row(engine, identifier) == original
    finally:
        engine.dispose()


def test_historical_null_deadline_stops_upgrade_without_repairing_or_deleting_row(
    fresh_source_control_migration_database_url: URL, tmp_path: Path
) -> None:
    config = _config(fresh_source_control_migration_database_url)
    command.upgrade(config, f"model_gateway@{OLD}")
    engine = create_engine(fresh_source_control_migration_database_url)
    try:
        identifier, now = seed(engine, tmp_path)
        with engine.begin() as db:
            db.execute(text("SET LOCAL ROLE model_gateway_worker_rw"))
            db.execute(
                CLAIM,
                {"id": identifier, "token": str(uuid4()), "started": now, "deadline": None},
            )
        original = stored_row(engine, identifier)
        assert installed_heads(engine) == {OLD}
        with pytest.raises(DBAPIError) as caught:
            command.upgrade(config, f"model_gateway@{NEW}")
        assert getattr(caught.value.orig, "sqlstate", None) == "23514"
        assert installed_heads(engine) == {OLD}
        assert stored_row(engine, identifier) == original
        with engine.connect() as db:
            assert not db.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE "
                    "conrelid='model_gateway.connection_check'::regclass "
                    "AND conname='ck_model_check_deadline_present')"
                )
            ).scalar_one()
    finally:
        engine.dispose()


def test_worker_role_rejects_incomplete_fence_and_accepts_complete_control(
    fresh_source_control_migration_database_url: URL, tmp_path: Path
) -> None:
    config = _config(fresh_source_control_migration_database_url)
    command.upgrade(config, f"model_gateway@{NEW}")
    engine = create_engine(fresh_source_control_migration_database_url)
    try:
        identifier, now = seed(engine, tmp_path)
        values: dict[str, Any] = {
            "id": identifier,
            "token": str(uuid4()),
            "started": now,
            "deadline": now + timedelta(seconds=25),
        }
        original = stored_row(engine, identifier)
        for missing in ("token", "started", "deadline"):
            with pytest.raises(DBAPIError) as caught, engine.begin() as db:
                db.execute(text("SET LOCAL ROLE model_gateway_worker_rw"))
                db.execute(CLAIM, values | {missing: None})
            assert getattr(caught.value.orig, "sqlstate", None) == "23514"
            assert stored_row(engine, identifier) == original
        with engine.begin() as db:
            db.execute(text("SET LOCAL ROLE model_gateway_worker_rw"))
            assert db.execute(CLAIM, values).rowcount == 1
        with engine.connect() as db:
            assert (
                db.execute(
                    text("SELECT state FROM model_gateway.connection_check WHERE id=:id"),
                    {"id": identifier},
                ).scalar_one()
                == "RUNNING"
            )
    finally:
        engine.dispose()
