import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from control_plane.app.modules.model_gateway import process_connection_check
from control_plane.app.modules.model_gateway.domain.connections import CheckKind
from tests.model_gateway.test_connection_check_e2e import (
    BASE,
    accept,
    candidate,
    configured,
    worker,
)
from tests.model_gateway.test_probe import response
from tests.model_gateway.test_stream_probe import ControlledStream, chunk, event
from tests.model_gateway.test_thinking_probe import ANSWER, REASONING, thinking_chunk
from tests.source_control.test_v06_production_e2e import Journey, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database

pytestmark = pytest.mark.integration


def test_thinking_journey_preserves_identity_observation_and_history(
    journey: Journey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    source = ControlledStream([])
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        if body["stream"] is False:
            return response()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)

    dependencies = worker(handler)
    old_ids = []
    for kind in (CheckKind.BASIC_TEXT, CheckKind.STREAM_TEXT, CheckKind.STREAM_STOP):
        path, receipt = accept(journey, deployment, kind=kind, key=f"historical-{kind.value}")
        source = ControlledStream([event(chunk("old-answer", "stop")), event("[DONE]")])
        assert process_connection_check(receipt["id"], dependencies=dependencies)
        old_ids.append(receipt["id"])
    # Downgrade only this isolated fixture before a THINKING fact exists, then verify upgrade.
    config = Config("alembic.ini")
    command.downgrade(config, "0003_model_stream_checks")
    with journey.database.owner.connect() as db:
        rows_before = (
            db.execute(text("SELECT * FROM model_gateway.connection_check ORDER BY id"))
            .mappings()
            .all()
        )
        receipts_before = (
            db.execute(text("SELECT * FROM model_gateway.idempotency_record ORDER BY id"))
            .mappings()
            .all()
        )
    command.upgrade(config, "heads")
    with journey.database.owner.connect() as db:
        rows_after = (
            db.execute(text("SELECT * FROM model_gateway.connection_check ORDER BY id"))
            .mappings()
            .all()
        )
        assert [{key: row[key] for key in rows_before[0]} for row in rows_after] == [
            dict(row) for row in rows_before
        ]
        assert (
            db.execute(text("SELECT * FROM model_gateway.idempotency_record ORDER BY id"))
            .mappings()
            .all()
            == receipts_before
        )
    for check_id in old_ids:
        assert not process_connection_check(check_id, dependencies=dependencies)
    assert len(calls) == 3
    path, receipt = accept(
        journey, deployment, kind=CheckKind.THINKING, key="thinking-fixed-intent"
    )
    assert receipt["checkKind"] == "THINKING" and receipt["state"] == "QUEUED"
    assert (
        _write(
            journey.admin,
            path,
            {"checkKind": "BASIC_TEXT"},
            etag='"v1"',
            key="thinking-fixed-intent",
            status=409,
        ).json()["code"]
        == "IDEMPOTENCY_CONFLICT"
    )
    _write(journey.admin, path, {"checkKind": "THINKING"}, etag='"v1"', status=409)
    source = ControlledStream(
        [event(thinking_chunk(REASONING)), event(chunk(ANSWER, "stop")), event("[DONE]")]
    )
    assert process_connection_check(receipt["id"], dependencies=dependencies)
    value = journey.admin.get(f"{path}/{receipt['id']}").json()
    assert value["state"] == "SUCCEEDED" and value["currentness"] == "CURRENT"
    assert value["revision"] == 3 and value["input"]["deploymentRevision"] == 1
    assert value["input"]["probeVersion"] == "thinking-v1"
    metadata = value["observation"]
    assert metadata["kind"] == "THINKING" and metadata["reasoningObserved"] is True
    assert metadata["reasoningDeltaCount"] == 1 and metadata["reasoningBytes"] == len(
        REASONING.encode()
    )
    assert metadata["textBytes"] == len(ANSWER.encode())
    assert metadata["localStreamClosed"] and metadata["completionMarkerObserved"]
    assert metadata["providerCancellation"] == "UNCONFIRMED"
    assert source.closed and len(calls) == 4
    assert not process_connection_check(receipt["id"], dependencies=dependencies)
    assert journey.admin.get(f"{BASE}/{deployment['id']}").json() == deployment
    assert (
        _write(
            journey.admin,
            path,
            {"checkKind": "THINKING"},
            etag='"v1"',
            key="thinking-fixed-intent",
            status=202,
        ).json()
        == receipt
    )
    _, missing = accept(journey, deployment, kind=CheckKind.THINKING)
    source = ControlledStream([event(chunk(ANSWER, "stop")), event("[DONE]")])
    assert process_connection_check(missing["id"], dependencies=dependencies)
    missing_value = journey.admin.get(f"{path}/{missing['id']}").json()
    assert (
        missing_value["state"] == "FAILED" and missing_value["reason"] == "THINKING_SIGNAL_MISSING"
    )
    assert missing_value["observation"]["reasoningObserved"] is False
    with journey.database.owner.connect() as db:
        persisted = (
            db.execute(
                text("SELECT * FROM model_gateway.connection_check WHERE id=:id"),
                {"id": receipt["id"]},
            )
            .mappings()
            .one()
        )
        audits = db.execute(
            text(
                "SELECT action,reason FROM audit.audit_event "
                "WHERE target_id=:id ORDER BY occurred_at"
            ),
            {"id": receipt["id"]},
        ).all()
    assert len(audits) == 2 and all('"checkKind": "THINKING"' in row.reason for row in audits)
    for private in (REASONING, ANSWER, "Compute 17 times 19"):
        assert private not in json.dumps(value)
        assert private not in repr(persisted)
        assert private not in repr(audits)
    # Invalid observation/kind combinations are rejected even at the restricted SQL boundary.
    for target_kind, reason_delta, reason_bytes in (
        ("BASIC_TEXT", 1, 1),
        ("THINKING", None, 1),
        ("THINKING", 257, 1),
        ("THINKING", 1, 65537),
    ):
        with journey.database.owner.connect() as db:
            transaction = db.begin()
            parameters = {
                "id": str(uuid4()),
                "deployment": deployment["id"],
                "kind": target_kind,
                "deltas": reason_delta,
                "bytes": reason_bytes,
                "thinking": target_kind == "THINKING",
            }
            try:
                db.execute(
                    text("""
                    INSERT INTO model_gateway.connection_check
                    (id,deployment_id,revision,requested_by,requested_at,input,connection_ref,
                     check_kind,state,attempt,material_currentness)
                    VALUES (:id,:deployment,1,'test',now(),
                        '{"connection_ref":"model-connection:trial"}'::jsonb,
                        'model-connection:trial',:kind,'QUEUED',0,'UNVERIFIABLE')
                """),
                    parameters,
                )
                db.execute(
                    text("""
                    UPDATE model_gateway.connection_check
                    SET state='RUNNING',revision=2,attempt=1,execution_token=:id,
                        started_at=now(),deadline_at=now()+interval '25 seconds'
                    WHERE id=:id
                """),
                    parameters,
                )
                with pytest.raises(DBAPIError):
                    db.execute(
                        text("""
                        UPDATE model_gateway.connection_check
                        SET state='FAILED',reason='INVALID_STREAM_RESPONSE',revision=3,
                            finished_at=now(),observed_bytes=1,observed_text=false,
                            observed_normal_completion=false,
                            observed_data_events=CASE WHEN :thinking THEN 1 END,
                            observed_text_deltas=CASE WHEN :thinking THEN 0 END,
                            observed_text_bytes=CASE WHEN :thinking THEN 0 END,
                            observed_completion_marker=CASE WHEN :thinking THEN false END,
                            observed_local_closed=CASE WHEN :thinking THEN true END,
                            provider_cancellation=CASE WHEN :thinking THEN 'UNCONFIRMED' END,
                            observed_reasoning=true,observed_reasoning_deltas=:deltas,
                            observed_reasoning_bytes=:bytes
                        WHERE id=:id
                    """),
                        parameters,
                    )
            finally:
                transaction.rollback()
    with pytest.raises(DBAPIError), journey.database.engines["model_gateway"].begin() as db:
        db.execute(
            text("UPDATE model_gateway.connection_check SET observed_reasoning=false WHERE id=:id"),
            {"id": receipt["id"]},
        )
    with pytest.raises(DBAPIError, match="thinking check facts prevent downgrade"):
        command.downgrade(config, "0003_model_stream_checks")
    _write(
        journey.admin,
        f"{BASE}/{deployment['id']}",
        {"displayName": "changed"},
        method="PATCH",
        etag='"v1"',
    )
    assert (
        "CANDIDATE_CHANGED"
        in journey.admin.get(f"{path}/{receipt['id']}").json()["currentnessReasons"]
    )
