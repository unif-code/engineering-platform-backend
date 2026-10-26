"""Domain and exact effective CHECK expressions; SQLite evidence is not PostgreSQL evidence."""

import ast
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

import control_plane.app.modules.model_gateway.domain.checks as checks_domain
from control_plane.app.modules.model_gateway.domain.checks import ConnectionCheck
from tests.model_gateway.dispatch_support import check_dispatch


def parenthesized(text: str, marker: str) -> str:
    start = text.index("(", text.index(marker))
    depth = 0
    for end in range(start, len(text)):
        depth += (text[end] == "(") - (text[end] == ")")
        if depth == 0:
            return text[start + 1 : end]
    raise AssertionError("complete CHECK expression required")


def effective_fence_predicate() -> str:
    migrations = Path(checks_domain.__file__).resolve().parents[5] / "migrations/model_gateway"
    old = ast.parse((migrations / "0002_model_connection_checks.py").read_text())
    ddl = next(
        node.value
        for node in ast.walk(old)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "CREATE TABLE model_gateway.connection_check" in node.value
    )
    predicates = [parenthesized(ddl, "CHECK ((attempt=0")]
    assert "connection_ref IS NOT NULL" in predicates[0]
    # The same regression runs on the immutable pre-repair tree, where this migration
    # does not exist and the effective old CHECK must fail the deadline assertion.
    addition = migrations / "0009_model_check_deadline.py"
    if addition.exists():
        tree = ast.parse(addition.read_text())
        ddl = next(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "ADD CONSTRAINT ck_model_check_deadline_present" in node.value
        )
        predicates.append(parenthesized(ddl, "CHECK ("))
    return " AND ".join(f"({predicate})" for predicate in predicates)


def fence_values(tmp_path: Path, *, attempted: bool = True) -> dict[str, Any]:
    h = check_dispatch(tmp_path)
    value: dict[str, Any] = h.store.checks[h.identifier].model_dump()
    if attempted:
        value.update(
            state="RUNNING",
            revision=2,
            attempt=1,
            execution_token=str(uuid4()),
            started_at=h.now,
            deadline_at=h.now + timedelta(seconds=25),
        )
    return value


def sql_accepts(value: dict[str, Any]) -> bool:
    row = (
        value["attempt"],
        value["execution_token"],
        value["started_at"].isoformat() if value["started_at"] is not None else None,
        value["deadline_at"].isoformat() if value["deadline_at"] is not None else None,
        value["input"]["connection_ref"],
    )
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE fence (attempt INTEGER NOT NULL, execution_token TEXT, "
            "started_at TEXT, deadline_at TEXT, connection_ref TEXT, CHECK ("
            + effective_fence_predicate()
            + "))"
        )
        try:
            db.execute("INSERT INTO fence VALUES (?,?,?,?,?)", row)
        except sqlite3.IntegrityError:
            return False
        return True


@pytest.mark.parametrize("attempted", [False, True])
def test_valid_unattempted_and_complete_fences_remain_accepted(
    tmp_path: Path, attempted: bool
) -> None:
    value = fence_values(tmp_path, attempted=attempted)
    assert ConnectionCheck.model_validate(value).attempt == int(attempted)
    assert sql_accepts(value)


@pytest.mark.parametrize(
    "missing", ["execution_token", "started_at", "deadline_at", "connection_ref"]
)
def test_domain_rejects_incomplete_execution_fence(tmp_path: Path, missing: str) -> None:
    value = fence_values(tmp_path)
    if missing == "connection_ref":
        value["input"]["connection_ref"] = None
    else:
        value[missing] = None
    with pytest.raises(ValidationError):
        ConnectionCheck.model_validate(value)


@pytest.mark.parametrize(
    "missing", ["execution_token", "started_at", "deadline_at", "connection_ref"]
)
def test_effective_sql_rejects_incomplete_execution_fence(tmp_path: Path, missing: str) -> None:
    value = fence_values(tmp_path)
    if missing == "connection_ref":
        value["input"]["connection_ref"] = None
    else:
        value[missing] = None
    assert not sql_accepts(value), "effective fence CHECK accepted a missing required part"


@pytest.mark.parametrize("seconds", [0, -1])
def test_deadline_must_follow_started_at(tmp_path: Path, seconds: int) -> None:
    value = fence_values(tmp_path)
    value["deadline_at"] = value["started_at"] + timedelta(seconds=seconds)
    with pytest.raises(ValidationError):
        ConnectionCheck.model_validate(value)
    assert not sql_accepts(value)


def test_unattempted_check_cannot_carry_a_token(tmp_path: Path) -> None:
    value = fence_values(tmp_path, attempted=False)
    value["execution_token"] = str(uuid4())
    with pytest.raises(ValidationError):
        ConnectionCheck.model_validate(value)
    assert not sql_accepts(value)


@pytest.mark.parametrize("attempted,state", [(False, "RUNNING"), (True, "QUEUED")])
def test_state_cannot_disagree_with_execution_attempt(
    tmp_path: Path, attempted: bool, state: str
) -> None:
    value = fence_values(tmp_path, attempted=attempted)
    value["state"] = state
    with pytest.raises(ValidationError):
        ConnectionCheck.model_validate(value)
