import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from control_plane.app.modules.model_gateway import process_connection_check, recover_expired_checks
from control_plane.app.modules.model_gateway.domain.connections import CheckKind
from tests.model_gateway.test_connection_check_e2e import (
    BASE,
    accept,
    candidate,
    configured,
    worker,
)
from tests.model_gateway.test_probe import CONNECTION, response
from tests.model_gateway.test_search_sources_probe import search_response
from tests.model_gateway.test_stream_probe import ControlledStream, event
from tests.model_gateway.test_thinking_probe import thinking_chunk
from tests.source_control.test_v06_production_e2e import Journey, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database

pytestmark = pytest.mark.integration


def test_search_source_journey_admission_snapshots_references_and_history(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, manifest = configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/responses"):
            return response(search_response())
        body = json.loads(request.content)
        if not body["stream"]:
            return response()
        parts = [event(thinking_chunk("synthetic reasoning", "answer", "stop")), event("[DONE]")]
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=ControlledStream(parts)
        )

    dependencies = worker(handler)
    old = []
    for kind in (
        CheckKind.BASIC_TEXT,
        CheckKind.STREAM_TEXT,
        CheckKind.STREAM_STOP,
        CheckKind.THINKING,
    ):
        path, receipt = accept(journey, deployment, kind=kind, key=f"search-history-{kind.value}")
        assert process_connection_check(receipt["id"], dependencies=dependencies)
        old.append(receipt)
    config = Config("alembic.ini")
    command.downgrade(config, "0004_model_thinking_checks")
    with journey.database.owner.connect() as db:
        before = (
            db.execute(text("SELECT * FROM model_gateway.connection_check ORDER BY id"))
            .mappings()
            .all()
        )
        receipts = (
            db.execute(text("SELECT * FROM model_gateway.idempotency_record ORDER BY id"))
            .mappings()
            .all()
        )
    command.upgrade(config, "heads")
    with journey.database.owner.connect() as db:
        after = (
            db.execute(text("SELECT * FROM model_gateway.connection_check ORDER BY id"))
            .mappings()
            .all()
        )
        assert [{key: row[key] for key in before[0]} for row in after] == [
            dict(row) for row in before
        ]
        assert (
            db.execute(text("SELECT * FROM model_gateway.idempotency_record ORDER BY id"))
            .mappings()
            .all()
            == receipts
        )
    path, blocked = accept(journey, deployment, kind=CheckKind.SEARCH_SOURCES)
    assert blocked["state"] == "BLOCKED" and blocked["reason"] == "SEARCH_MODEL_NOT_ALLOWED"
    assert not process_connection_check(blocked["id"], dependencies=dependencies)
    assert len(calls) == 4
    approved = CONNECTION | {"responsesSearchModelIds": ["synthetic-model"]}
    manifest.write_text(json.dumps({"environment": "TEST", "connections": [approved]}))
    for receipt in old:
        assert journey.admin.get(f"{path}/{receipt['id']}").json()["currentness"] == "CURRENT"
        assert not process_connection_check(receipt["id"], dependencies=dependencies)
        assert (
            _write(
                journey.admin,
                path,
                {"checkKind": receipt["checkKind"]},
                etag='"v1"',
                key=f"search-history-{receipt['checkKind']}",
                status=202,
            ).json()
            == receipt
        )
    _, accepted = accept(
        journey, deployment, kind=CheckKind.SEARCH_SOURCES, key="search-new-request"
    )
    _write(
        journey.admin,
        path,
        {"checkKind": "THINKING"},
        etag='"v1"',
        key="search-new-request",
        status=409,
    )
    assert process_connection_check(accepted["id"], dependencies=dependencies)
    fetched = journey.admin.get(f"{path}/{accepted['id']}")
    assert fetched.status_code == 200
    value = fetched.json()
    assert value["state"] == "SUCCEEDED" and value["currentness"] == "CURRENT"
    assert value["input"]["adapterVersion"] == "bailian-responses-v1"
    assert value["observation"]["protocol"] == "BAILIAN_RESPONSES_V1"
    assert value["observation"]["sources"] == [
        {"callId": "search-1", "sanitizedUrl": "https://www.alibabacloud.com/help"}
    ]
    assert value["observation"]["queries"][0]["count"] == 1
    assert value["observation"]["providerSearchCallCount"] is None
    assert value["observation"]["localResponseClosed"] is True
    assert len(calls) == 5 and not process_connection_check(
        accepted["id"], dependencies=dependencies
    )
    assert journey.admin.get(f"{BASE}/{deployment['id']}").json() == deployment
    assert (
        _write(
            journey.admin,
            path,
            {"checkKind": "SEARCH_SOURCES"},
            etag='"v1"',
            key="search-new-request",
            status=202,
        ).json()
        == accepted
    )
    with journey.database.owner.connect() as db:
        row = (
            db.execute(
                text("SELECT * FROM model_gateway.connection_check WHERE id=:id"),
                {"id": accepted["id"]},
            )
            .mappings()
            .one()
        )
        audits = db.execute(
            text("SELECT action,reason FROM audit.audit_event WHERE target_id=:id"),
            {"id": accepted["id"]},
        ).all()
    assert len(audits) == 2
    for private in ("synthetic private query", "synthetic private answer", "tracking=private"):
        assert (
            private not in repr(row) and private not in repr(audits) and private not in fetched.text
        )
    assert "https://" not in repr(audits)
    # New admission affects only the search snapshot, not the four existing protocol kinds.
    _, queued = accept(journey, deployment, kind=CheckKind.SEARCH_SOURCES)
    manifest.write_text(json.dumps({"environment": "TEST", "connections": [CONNECTION]}))
    assert not process_connection_check(queued["id"], dependencies=dependencies)
    assert (
        journey.admin.get(f"{path}/{queued['id']}").json()["reason"] == "SEARCH_MODEL_NOT_ALLOWED"
    )
    assert journey.admin.get(f"{path}/{accepted['id']}").json()["currentness"] == "STALE"
    for receipt in old:
        assert journey.admin.get(f"{path}/{receipt['id']}").json()["currentness"] == "CURRENT"
    manifest.write_text(
        json.dumps({"environment": "TEST", "connections": [approved | {"region": "us-east-1"}]})
    )
    _, region = accept(journey, deployment, kind=CheckKind.SEARCH_SOURCES)
    assert region["state"] == "BLOCKED" and region["reason"] == "SEARCH_REGION_NOT_ALLOWED"
    assert len(calls) == 5
    with pytest.raises(DBAPIError), journey.database.engines["model_gateway"].begin() as db:
        db.execute(
            text("UPDATE model_gateway.connection_check SET search_sources='[]' WHERE id=:id"),
            {"id": accepted["id"]},
        )
    with pytest.raises(DBAPIError, match="search source facts prevent downgrade"):
        command.downgrade(config, "0004_model_thinking_checks")


def test_search_result_and_reference_rollback_never_resends_provider(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, manifest = configured(tmp_path, monkeypatch)
    manifest.write_text(
        json.dumps(
            {
                "environment": "TEST",
                "connections": [CONNECTION | {"responsesSearchModelIds": ["synthetic-model"]}],
            }
        )
    )
    deployment = candidate(journey)
    path, receipt = accept(journey, deployment, kind=CheckKind.SEARCH_SOURCES)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(search_response())

    dependencies = worker(handler)

    class FailingAudit:
        def append_in_transaction(self, db: object, envelope: object) -> None:
            raise RuntimeError("synthetic audit failure")

    broken = replace(dependencies, common=replace(dependencies.common, audit=FailingAudit()))
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        process_connection_check(receipt["id"], dependencies=broken)
    assert len(calls) == 1
    with journey.database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT state,search_sources,search_queries FROM model_gateway.connection_check "
                "WHERE id=:id"
            ),
            {"id": receipt["id"]},
        ).one()
        assert tuple(row) == ("RUNNING", None, None)
    assert not process_connection_check(receipt["id"], dependencies=dependencies)
    later = dependencies.common.now() + timedelta(seconds=30)
    recovered = replace(dependencies, common=replace(dependencies.common, now=lambda: later))
    assert recover_expired_checks(dependencies=recovered, limit=10) == 1
    assert journey.admin.get(f"{path}/{receipt['id']}").json()["state"] == "UNKNOWN"
    assert len(calls) == 1
