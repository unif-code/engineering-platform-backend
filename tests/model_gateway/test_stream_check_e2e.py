import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.model_gateway import process_connection_check
from control_plane.app.modules.model_gateway.adapters import SqlAlchemyDeploymentRepository
from control_plane.app.modules.model_gateway.domain.checks import CheckInputSnapshot
from control_plane.app.modules.model_gateway.domain.connections import (
    CheckKind,
    ConnectionDefinition,
    digest,
    probe_body,
)
from control_plane.app.shared.idempotency import IdempotentResponse, execute_idempotent
from tests.model_gateway.test_connection_check_e2e import (
    BASE,
    accept,
    candidate,
    configured,
    worker,
)
from tests.model_gateway.test_probe import CONNECTION, response
from tests.model_gateway.test_stream_probe import ControlledStream, chunk, event
from tests.source_control.test_v06_production_e2e import Journey, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database

pytestmark = pytest.mark.integration


def test_fixed_kinds_use_the_same_queue_and_preserve_local_stop_evidence(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path, full_receipt = accept(
        journey, deployment, kind=CheckKind.STREAM_TEXT, key="fixed-stream-request"
    )
    assert full_receipt["checkKind"] == "STREAM_TEXT"
    _write(
        journey.admin,
        path,
        {"checkKind": "STREAM_STOP"},
        etag='"v1"',
        key="fixed-stream-request",
        status=409,
    )
    _write(journey.admin, path, {"checkKind": "STREAM_STOP"}, etag='"v1"', status=409)
    _write(journey.admin, path, {}, etag='"v1"', status=422)
    source = ControlledStream(
        [event(chunk()) + event(chunk("text")) + event(chunk("", "stop")) + event("[DONE]")]
    )
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)

    dependencies = worker(handler)
    assert process_connection_check(full_receipt["id"], dependencies=dependencies)
    full = journey.admin.get(f"{path}/{full_receipt['id']}").json()
    assert full["checkKind"] == full["observation"]["kind"] == "STREAM_TEXT"
    assert full["state"] == "SUCCEEDED" and full["currentness"] == "CURRENT"
    assert full["input"]["probeVersion"] == "stream-text-v1"
    assert full["observation"]["completionMarkerObserved"]
    assert full["observation"]["providerCancellation"] == "UNCONFIRMED"
    assert source.closed
    assert not process_connection_check(full_receipt["id"], dependencies=dependencies)
    _, stop_receipt = accept(journey, deployment, kind=CheckKind.STREAM_STOP)
    source = ControlledStream(
        [
            event(chunk()),
            event(chunk("first")),
            event(chunk("not consumed", "stop")),
            event("[DONE]"),
        ]
    )
    assert process_connection_check(stop_receipt["id"], dependencies=dependencies)
    stopped = journey.admin.get(f"{path}/{stop_receipt['id']}").json()
    assert stopped["state"] == "SUCCEEDED" and stopped["checkKind"] == "STREAM_STOP"
    assert stopped["observation"]["localStreamClosed"]
    assert not stopped["observation"]["completionMarkerObserved"]
    assert stopped["observation"]["providerCancellation"] == "UNCONFIRMED"
    assert stopped["usage"] is None and source.closed and source.consumed == 2
    assert len(sends) == 2
    assert "not consumed" not in json.dumps(stopped)
    replay = _write(
        journey.admin,
        path,
        {"checkKind": "STREAM_TEXT"},
        etag='"v1"',
        key="fixed-stream-request",
        status=202,
    ).json()
    assert replay == full_receipt
    assert journey.admin.get(f"{BASE}/{deployment['id']}").json() == deployment
    history = journey.admin.get(path).json()["items"]
    assert {row["checkKind"] for row in history} == {"STREAM_TEXT", "STREAM_STOP"}
    assert len({row["input"]["inputDigest"] for row in history}) == 2
    # A different kind's probe version must not stale this evidence.
    import control_plane.app.modules.model_gateway.domain.connections as definitions

    monkeypatch.setitem(definitions.PROBE_VERSIONS, CheckKind.BASIC_TEXT, "basic-text-v2")
    assert journey.admin.get(f"{path}/{full_receipt['id']}").json()["currentness"] == "CURRENT"
    monkeypatch.setitem(definitions.PROBE_VERSIONS, CheckKind.STREAM_TEXT, "stream-text-v2")
    assert (
        "PROBE_CHANGED"
        in journey.admin.get(f"{path}/{full_receipt['id']}").json()["currentnessReasons"]
    )
    with journey.database.owner.connect() as db:
        reasons = list(
            db.execute(
                text("SELECT reason FROM audit.audit_event WHERE target_id=:id"),
                {"id": stop_receipt["id"]},
            ).scalars()
        )
    assert len(reasons) == 2 and all('"checkKind": "STREAM_STOP"' in value for value in reasons)


def test_upgrade_preserves_basic_history_frozen_input_receipt_and_unsent_request(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    config = Config("alembic.ini")
    command.downgrade(config, "0002_model_connection_checks")
    now = datetime.now(UTC)
    terminal_id, queued_id = str(uuid4()), str(uuid4())
    runtime = bootstrap.model_gateway_http_runtime()
    with runtime.engine.connect() as db:
        original = SqlAlchemyDeploymentRepository(db).get(deployment["id"])
    assert original is not None
    frozen = CheckInputSnapshot.capture(
        original, ConnectionDefinition.model_validate(CONNECTION), "TEST", CheckKind.BASIC_TEXT
    ).model_dump(mode="json")
    frozen["input_digest"] = digest(
        {
            "input": {key: value for key, value in frozen.items() if key != "input_digest"},
            "probe": probe_body("synthetic-model", CheckKind.BASIC_TEXT),
        }
    )
    receipt = {
        "id": terminal_id,
        "deploymentId": original.id,
        "candidateRevision": 1,
        "revision": 1,
        "state": "BLOCKED",
        "reason": "MATERIAL_UNAVAILABLE",
        "requestedAt": now.isoformat(),
    }
    try:
        with journey.database.owner.begin() as db:
            for check_id, state, reason, finished in (
                (terminal_id, "BLOCKED", "MATERIAL_UNAVAILABLE", now),
                (queued_id, "QUEUED", None, None),
            ):
                db.execute(
                    text("""
                    INSERT INTO model_gateway.connection_check
                    (id,deployment_id,revision,requested_by,requested_at,input,connection_ref,state,reason,
                     attempt,finished_at,material_currentness)
                    VALUES (:id,:deployment_id,1,:actor,:now,CAST(:input AS JSONB),
                            'model-connection:trial',
                            :state,:reason,0,:finished,'UNVERIFIABLE')
                """),
                    {
                        "id": check_id,
                        "deployment_id": original.id,
                        "actor": journey.admin.get("/api/v1/me").json()["accountId"],
                        "now": now,
                        "input": json.dumps(frozen),
                        "state": state,
                        "reason": reason,
                        "finished": finished,
                    },
                )
        with runtime.engine.begin() as db:
            execute_idempotent(
                SqlAlchemyDeploymentRepository(db),
                actor="migration-fixture",
                operation="model_connection_check.create",
                key="frozen-basic-receipt",
                fingerprint="legacy-fingerprint",
                command=lambda: IdempotentResponse(status_code=202, body=receipt),
                now=lambda: now,
                new_id=uuid4,
                idempotency_sealing_key=runtime.secret_manager.load().idempotency_sealing_key,
            )
        with journey.database.owner.connect() as db:
            frozen_before = db.execute(
                text(
                    "SELECT input,state,revision FROM model_gateway.connection_check WHERE id=:id"
                ),
                {"id": terminal_id},
            ).one()
            receipt_before = db.execute(
                text(
                    "SELECT request_fingerprint,sealed_response "
                    "FROM model_gateway.idempotency_record "
                    "WHERE idempotency_key='frozen-basic-receipt'"
                )
            ).one()
        command.upgrade(config, "heads")
        with journey.database.owner.connect() as db:
            assert (
                db.execute(
                    text(
                        "SELECT input,state,revision FROM model_gateway.connection_check "
                        "WHERE id=:id"
                    ),
                    {"id": terminal_id},
                ).one()
                == frozen_before
            )
            assert (
                db.execute(
                    text(
                        "SELECT request_fingerprint,sealed_response "
                        "FROM model_gateway.idempotency_record "
                        "WHERE idempotency_key='frozen-basic-receipt'"
                    )
                ).one()
                == receipt_before
            )
        path = f"{BASE}/{original.id}/connection-checks"
        history = journey.admin.get(f"{path}/{terminal_id}").json()
        assert history["checkKind"] == "BASIC_TEXT" and history["observation"] is None
        assert history["input"]["inputDigest"] == frozen["input_digest"]
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert json.loads(request.content)["stream"] is False
            return response()

        dependencies = worker(handler)
        assert not process_connection_check(terminal_id, dependencies=dependencies)
        assert process_connection_check(queued_id, dependencies=dependencies)
        assert not process_connection_check(queued_id, dependencies=dependencies)
        assert len(calls) == 1
        completed = journey.admin.get(f"{path}/{queued_id}").json()
        assert completed["checkKind"] == "BASIC_TEXT" and completed["state"] == "SUCCEEDED"
        assert completed["observation"]["kind"] == "BASIC_TEXT"
    finally:
        command.upgrade(config, "heads")
