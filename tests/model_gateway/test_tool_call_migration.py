"""Preserve old facts through upgrade/rollback and reject lossy rollback; PostgreSQL CI."""

from datetime import timedelta
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
from control_plane.app.modules.model_gateway.adapters.dossiers import (
    SqlAlchemyValidationDossierRepository,
)
from control_plane.app.modules.model_gateway.domain.connections import CheckKind, digest
from control_plane.app.modules.model_gateway.domain.dossiers import ValidationDossier
from tests.model_gateway.dispatch_support import check_dispatch
from tests.source_control.test_migration import _config
from tests.source_control.test_migration import (
    fresh_source_control_migration_database_url as fresh_source_control_migration_database_url,
)

pytestmark = pytest.mark.integration
OLD = "0009_model_check_deadline"
NEW = "0010_model_tool_calls"


def heads(engine: Engine) -> set[str]:
    with engine.connect() as db:
        return set(db.execute(text("SELECT version_num FROM alembic_version")).scalars())


def stored(engine: Engine, check_id: str, dossier_id: str) -> tuple[Any, Any]:
    with engine.connect() as db:
        check = db.execute(
            text(
                "SELECT (to_jsonb(c)-'observed_matching_call'"
                "-'observed_tool_call_completion')::text "
                "FROM model_gateway.connection_check c WHERE id=:id"
            ),
            {"id": check_id},
        ).scalar_one()
        dossier = db.execute(
            text(
                "SELECT snapshot_text,snapshot_hash FROM model_gateway.validation_dossier "
                "WHERE id=:id"
            ),
            {"id": dossier_id},
        ).one()
        return check, tuple(dossier)


def test_old_check_and_declared_snapshot_bytes_survive_round_trip(
    fresh_source_control_migration_database_url: URL, tmp_path: Path
) -> None:
    config = _config(fresh_source_control_migration_database_url)
    command.upgrade(config, f"model_gateway@{OLD}")
    engine = create_engine(fresh_source_control_migration_database_url)
    try:
        h = check_dispatch(tmp_path)
        payload = {
            "id": str(uuid4()),
            "deployment_id": h.store.candidate.id,
            "candidate_revision": 1,
            "created_by": "admin",
            "created_at": h.now.isoformat().replace("+00:00", "Z"),
            "checks": [],
            "materials": [
                {
                    "category": "MODEL_IDENTITY",
                    "title": "Declared",
                    "source_reference": "urn:synthetic:migration",
                    "external_version": None,
                    "declared_content_sha256": None,
                    "expires_at": None,
                    "note": None,
                    "provenance": "DECLARED",
                }
            ],
        }
        dossier = ValidationDossier.model_validate(payload | {"snapshot_hash": digest(payload)})
        with engine.begin() as db:
            assert SqlAlchemyDeploymentRepository(db).insert(h.store.candidate) is not None
            assert SqlAlchemyCheckRepository(db).insert_check(h.store.checks[h.identifier])
            SqlAlchemyValidationDossierRepository(db).insert_dossier(dossier)
        with engine.begin() as db:
            db.execute(text("SET LOCAL ROLE model_gateway_worker_rw"))
            db.execute(
                text("""
                UPDATE model_gateway.connection_check SET state='RUNNING',revision=2,
                    attempt=1,execution_token=:token,started_at=:started,deadline_at=:deadline,
                    material_currentness='CURRENT' WHERE id=:id
            """),
                {
                    "id": h.identifier,
                    "token": str(uuid4()),
                    "started": h.now,
                    "deadline": h.now + timedelta(seconds=25),
                },
            )
            db.execute(
                text("""
                UPDATE model_gateway.connection_check SET state='SUCCEEDED',revision=3,
                    finished_at=:finished,observed_bytes=16,observed_text=true,
                    observed_normal_completion=true,provider_request_id='chatcmpl-old',
                    reported_model_id='synthetic-model',elapsed_ms=1 WHERE id=:id
            """),
                {"id": h.identifier, "finished": h.now + timedelta(seconds=1)},
            )
        original = stored(engine, h.identifier, dossier.id)
        command.upgrade(config, f"model_gateway@{NEW}")
        assert heads(engine) == {NEW}
        assert stored(engine, h.identifier, dossier.id) == original
        with engine.connect() as db:
            loaded = SqlAlchemyCheckRepository(db).check(h.identifier)
            assert loaded is not None and loaded.observation is not None
            assert loaded.observation.model_dump() == {
                "kind": CheckKind.BASIC_TEXT,
                "consumed_bytes": 16,
                "text_observed": True,
                "normal_completion_observed": True,
            }
        command.downgrade(config, f"model_gateway@{OLD}")
        assert heads(engine) == {OLD}
        assert stored(engine, h.identifier, dossier.id) == original
        command.upgrade(config, f"model_gateway@{NEW}")
        assert stored(engine, h.identifier, dossier.id) == original
    finally:
        engine.dispose()


def test_even_blocked_tool_fact_refuses_downgrade_without_deletion(
    fresh_source_control_migration_database_url: URL, tmp_path: Path
) -> None:
    config = _config(fresh_source_control_migration_database_url)
    command.upgrade(config, f"model_gateway@{NEW}")
    engine = create_engine(fresh_source_control_migration_database_url)
    try:
        h = check_dispatch(tmp_path)
        h.store.checks.clear()
        with h.store.begin():
            blocked = h.service.accept(
                h.store.candidate.id,
                expected_revision=1,
                actor="admin",
                check_kind=CheckKind.TOOL_CALL,
            )
        assert blocked.state == "BLOCKED"
        with engine.begin() as db:
            assert SqlAlchemyDeploymentRepository(db).insert(h.store.candidate) is not None
            assert SqlAlchemyCheckRepository(db).insert_check(blocked)
        with engine.connect() as db:
            before = db.execute(
                text("SELECT to_jsonb(c)::text FROM model_gateway.connection_check c WHERE id=:id"),
                {"id": blocked.id},
            ).scalar_one()
        with pytest.raises(DBAPIError) as failed:
            command.downgrade(config, f"model_gateway@{OLD}")
        assert getattr(failed.value.orig, "sqlstate", None) == "P0001"
        assert heads(engine) == {NEW}
        with engine.connect() as db:
            assert (
                db.execute(
                    text(
                        "SELECT to_jsonb(c)::text FROM model_gateway.connection_check c "
                        "WHERE id=:id"
                    ),
                    {"id": blocked.id},
                ).scalar_one()
                == before
            )
    finally:
        engine.dispose()
