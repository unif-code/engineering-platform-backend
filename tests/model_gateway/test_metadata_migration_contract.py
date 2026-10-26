"""Exact migration predicate with SQLite three-valued logic, not PostgreSQL evidence."""

import ast
import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

import control_plane.app.modules.model_gateway.domain.metadata as metadata_domain
from control_plane.app.modules.model_gateway.application.metadata import metadata_admission
from control_plane.app.modules.model_gateway.domain.metadata import MetadataObservation
from tests.model_gateway.metadata_support import metadata_state


def fence_predicate() -> str:
    path = (
        Path(metadata_domain.__file__).resolve().parents[5]
        / "migrations/model_gateway/0008_model_metadata_observations.py"
    )
    tree = ast.parse(path.read_text())
    ddl = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "CREATE TABLE model_gateway.metadata_observation" in node.value
    )
    start = ddl.index("(", ddl.index("CHECK ((attempt=0"))
    depth = 0
    for end in range(start, len(ddl)):
        depth += (ddl[end] == "(") - (ddl[end] == ")")
        if depth == 0:
            return ddl[start + 1 : end]
    raise AssertionError("migration execution-fence CHECK must be complete")


@pytest.mark.parametrize("missing", [None, "execution_token", "started_at", "deadline_at"])
def test_exact_migration_fence_rejects_missing_parts_and_keeps_valid_control(
    missing: str | None,
) -> None:
    h = metadata_state(None)
    admission = metadata_admission(h.store.candidate, h.directory, h.sources)
    assert admission.source is not None
    with h.store.begin():
        queued = h.service.accept(
            h.store.candidate.id,
            expected_revision=1,
            source_fingerprint=admission.source.fingerprint,
            actor="synthetic-admin",
        )
    values = queued.model_dump() | {
        "state": "RUNNING",
        "revision": 2,
        "attempt": 1,
        "execution_token": str(uuid4()),
        "started_at": h.now,
        "deadline_at": h.now + timedelta(seconds=25),
    }
    if missing is not None:
        values[missing] = None
        with pytest.raises(ValidationError):
            MetadataObservation.model_validate(values)
    else:
        MetadataObservation.model_validate(values)
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE lease (attempt INTEGER NOT NULL, execution_token TEXT, "
            "started_at TEXT, deadline_at TEXT, CHECK (" + fence_predicate() + "))"
        )
        row = (
            1,
            values["execution_token"],
            values["started_at"].isoformat() if values["started_at"] is not None else None,
            values["deadline_at"].isoformat() if values["deadline_at"] is not None else None,
        )
        if missing is not None:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("INSERT INTO lease VALUES (?,?,?,?)", row)
        else:
            db.execute("INSERT INTO lease VALUES (?,?,?,?)", row)
            assert db.execute("SELECT count(*) FROM lease").fetchone()[0] == 1
