"""Real PostgreSQL/Session/worker journey with synthetic HTTP only; executed by CI."""

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.model_gateway import process_connection_check
from control_plane.app.modules.model_gateway.domain.connections import CheckKind
from tests.model_gateway.dispatch_support import successful_response
from tests.model_gateway.test_connection_check_e2e import (
    BASE,
    accept,
    candidate,
    configured,
    worker,
)
from tests.model_gateway.test_stream_probe import ControlledStream
from tests.model_gateway.tool_call_support import tool_response
from tests.source_control.test_v06_production_e2e import Journey, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database

pytestmark = pytest.mark.integration


def approve_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, manifest = configured(tmp_path, monkeypatch)
    value = json.loads(manifest.read_text())
    value["connections"][0]["toolCallModelIds"] = ["synthetic-model"]
    value["connections"][0]["responsesSearchModelIds"] = ["synthetic-model"]
    manifest.write_text(json.dumps(value))
    bootstrap.model_gateway_http_runtime.cache_clear()


def test_cleanup_unknown_and_original_receipt_survive_real_persistence(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_tools(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path, receipt = accept(journey, deployment, kind=CheckKind.TOOL_CALL, key="tool-close-original")
    calls: list[httpx.Request] = []

    class BrokenClose(ControlledStream):
        async def aclose(self) -> None:
            raise OSError("synthetic close failure")

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=BrokenClose([json.dumps(tool_response()).encode()]),
        )

    dependencies = worker(provider)
    assert process_connection_check(receipt["id"], dependencies=dependencies)
    detail = journey.admin.get(path + "/" + receipt["id"])
    assert detail.status_code == 200
    value = detail.json()
    assert value["state"] == "UNKNOWN" and value["reason"] == "RESPONSE_CLOSE_FAILED"
    assert value["observation"]["matchingCallObserved"]
    assert value["observation"]["toolCallCompletionObserved"]
    assert not value["observation"]["localResponseClosed"]
    assert (
        _write(
            journey.admin,
            path,
            {"checkKind": "TOOL_CALL"},
            etag='"v1"',
            key="tool-close-original",
            status=202,
        ).json()
        == receipt
    )
    assert not process_connection_check(receipt["id"], dependencies=dependencies)
    assert journey.admin.get(path + "/" + receipt["id"]).json() == value and len(calls) == 1


def test_six_real_kind_results_bind_to_one_declared_dossier_without_tool_execution(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_tools(tmp_path, monkeypatch)
    deployment = candidate(journey)
    identifiers: list[str] = []
    calls: list[httpx.Request] = []
    for kind in CheckKind:
        path, receipt = accept(journey, deployment, kind=kind)
        assert receipt["state"] == "QUEUED"

        def provider(request: httpx.Request, selected: CheckKind = kind) -> httpx.Response:
            calls.append(request)
            return successful_response(selected)

        assert process_connection_check(receipt["id"], dependencies=worker(provider))
        detail = journey.admin.get(path + "/" + receipt["id"])
        assert detail.status_code == 200 and detail.json()["state"] == "SUCCEEDED"
        if kind is CheckKind.TOOL_CALL:
            assert detail.json()["observation"]["matchingCallObserved"]
            assert detail.json()["observation"]["localResponseClosed"]
            assert "textObserved" not in detail.json()["observation"]
            assert json.loads(calls[-1].content)["parallel_tool_calls"] is False
        identifiers.append(receipt["id"])
    assert len(calls) == 6 and len(identifiers) == 6
    path = f"{BASE}/{deployment['id']}/validation-dossiers"
    receipt = _write(
        journey.admin, path, {"materials": [], "checkIds": identifiers}, etag='"v1"', status=201
    ).json()
    detail = journey.admin.get(path + "/" + receipt["id"])
    assert detail.status_code == 200, detail.text
    value = detail.json()
    assert len(value["snapshot"]["checks"]) == len(value["checkCoverage"]) == 6
    assert {check["checkKind"] for check in value["snapshot"]["checks"]} == {
        kind.value for kind in CheckKind
    }
    assert all(
        "observation" not in check and "observation" not in check["resultSummary"]
        for check in value["snapshot"]["checks"]
    )
    assert value["snapshot"]["snapshotHash"] == receipt["snapshotHash"]
    assert journey.admin.get(f"{BASE}/{deployment['id']}").json() == deployment
    assert len(calls) == 6
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE target_type='MODEL_CONNECTION_CHECK' AND target_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": identifiers},
            ).scalar_one()
            == 12
        )


def test_tool_columns_are_worker_only_and_success_requires_actual_complete_shape(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approve_tools(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path, receipt = accept(journey, deployment, kind=CheckKind.TOOL_CALL)
    now = bootstrap.model_gateway_http_runtime().dependencies.now()
    with pytest.raises(DBAPIError) as denied:
        with journey.database.engines["model_gateway"].begin() as db:
            db.execute(
                text(
                    "UPDATE model_gateway.connection_check "
                    "SET observed_matching_call=true WHERE id=:id"
                ),
                {"id": receipt["id"]},
            )
    assert getattr(denied.value.orig, "sqlstate", None) == "42501"
    with journey.database.engines["model_gateway_worker"].begin() as db:
        db.execute(
            text("""
            UPDATE model_gateway.connection_check SET state='RUNNING', revision=revision+1,
                attempt=1,execution_token=:token,started_at=:started,deadline_at=:deadline,
                material_currentness='CURRENT' WHERE id=:id
        """),
            {
                "id": receipt["id"],
                "token": str(uuid4()),
                "started": now,
                "deadline": now + timedelta(seconds=25),
            },
        )
    terminal = text("""
        UPDATE model_gateway.connection_check SET state='SUCCEEDED',revision=revision+1,
            finished_at=:finished,observed_bytes=100,observed_matching_call=:matched,
            observed_tool_call_completion=:completed,observed_local_closed=:closed,
            observed_text=:text WHERE id=:id
    """)
    values = {
        "id": receipt["id"],
        "finished": now + timedelta(seconds=1),
        "matched": True,
        "completed": True,
        "closed": True,
        "text": None,
    }
    invalid: list[dict[str, object]] = [
        {"matched": False},
        {"completed": False},
        {"closed": False},
        {"matched": None},
        {"text": True},
    ]
    for changed in invalid:
        with pytest.raises(DBAPIError) as failed:
            with journey.database.engines["model_gateway_worker"].begin() as db:
                db.execute(terminal, values | changed)
        assert getattr(failed.value.orig, "sqlstate", None) == "23514"
    with journey.database.engines["model_gateway_worker"].begin() as db:
        assert db.execute(terminal, values).rowcount == 1
    detail = journey.admin.get(path + "/" + receipt["id"])
    assert detail.status_code == 200 and detail.json()["observation"]["matchingCallObserved"]
    with pytest.raises(DBAPIError):
        with journey.database.engines["model_gateway_worker"].begin() as db:
            db.execute(
                text(
                    "UPDATE model_gateway.connection_check "
                    "SET revision=revision+1,observed_matching_call=false WHERE id=:id"
                ),
                {"id": receipt["id"]},
            )
