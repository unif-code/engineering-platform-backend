"""Default Session and independent worker with real PostgreSQL and a synthetic HTTP transport."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from io import StringIO
from pathlib import Path
from threading import Event
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.bootstrap.model_gateway_worker import model_check_worker_dependencies
from control_plane.app.modules.model_gateway import (
    ModelCheckWorkerDependencies,
    process_connection_check,
    recover_expired_checks,
)
from control_plane.app.modules.model_gateway.adapters.probe import HttpxModelProbe
from control_plane.tools import model_gateway_worker
from tests.model_gateway.test_catalog_contract import PAYLOAD
from tests.model_gateway.test_probe import CONNECTION, RESPONSE, material, public_dns, response
from tests.source_control.test_v06_production_e2e import Journey, _grant, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database
from tests.test_e2e_access_governance import SAME_ORIGIN

pytestmark = pytest.mark.integration
BASE = "/api/v1/admin/model-deployments"


def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "synthetic-provider-files"
    material(root)
    manifest = tmp_path / "connections.json"
    manifest.write_text(json.dumps({"environment": "TEST", "connections": [CONNECTION]}))
    monkeypatch.setenv("MODEL_GATEWAY_CONNECTIONS_PATH", str(manifest))
    monkeypatch.setenv("MODEL_GATEWAY_ENVIRONMENT", "TEST")
    monkeypatch.setenv("MODEL_GATEWAY_SECRET_REFERENCE_ROOT", str(root))
    bootstrap.model_gateway_http_runtime.cache_clear()
    return root, manifest


def candidate(
    journey: Journey, key: str = "check-candidate", **overrides: object
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _write(
            journey.admin,
            BASE,
            PAYLOAD
            | {
                "deploymentKey": key,
                "providerModelId": "synthetic-model",
                "connectionRef": "model-connection:trial",
            }
            | overrides,
            status=201,
        ).json(),
    )


def worker(handler: Callable[[httpx.Request], httpx.Response]) -> ModelCheckWorkerDependencies:
    return model_check_worker_dependencies(
        probe_factory=lambda root: HttpxModelProbe(
            root, transport=httpx.MockTransport(handler), resolver=public_dns
        )
    )


def accept(
    journey: Journey, deployment: dict[str, Any], *, key: str | None = None
) -> tuple[str, dict[str, Any]]:
    path = f"{BASE}/{deployment['id']}/connection-checks"
    receipt = _write(
        journey.admin, path, {}, etag=f'"v{deployment["revision"]}"', status=202, key=key
    )
    return path, receipt.json()


def test_default_check_journey_replay_history_currentness_and_restricted_roles(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, manifest = configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path, receipt = accept(journey, deployment, key="check-original-request")
    assert receipt["state"] == "QUEUED" and receipt["candidateRevision"] == receipt["revision"] == 1
    assert (
        _write(
            journey.admin, path, {}, etag='"v1"', status=202, key="check-original-request"
        ).json()
        == receipt
    )
    assert (
        _write(journey.admin, path, {}, etag='"v1"', status=409).json()["code"]
        == "MODEL_CONNECTION_CHECK_ACTIVE"
    )
    assert journey.admin.get(f"{BASE}/{deployment['id']}").headers["etag"] == '"v1"'
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(
            RESPONSE | {"usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
        )

    dependencies = worker(handler)
    out = StringIO()
    with redirect_stdout(out):
        assert (
            model_gateway_worker.main(["--limit", "10"], dependencies_provider=lambda: dependencies)
            == 0
        )
    assert json.loads(out.getvalue()) == {"processed": 1, "recovered": 0, "attempted": 1}
    detail = journey.admin.get(f"{path}/{receipt['id']}")
    value = detail.json()
    assert detail.status_code == 200 and detail.headers["etag"].startswith('"check-')
    assert value["state"] == "SUCCEEDED" and value["currentness"] == "CURRENT"
    assert value["revision"] == 3 and value["attempt"] == 1
    assert value["input"]["deploymentRevision"] == 1
    assert value["usage"] == {"promptTokens": 3, "completionTokens": 1, "totalTokens": 4}
    assert value["reportedModelId"] == "synthetic-model"
    assert not {"executionToken", "responseBody", "prompt", "secretRef"} & value.keys()
    assert "synthetic-only-secret" not in detail.text and "Reply with OK" not in detail.text
    assert not process_connection_check(receipt["id"], dependencies=dependencies)
    assert len(calls) == 1
    # Recreate the app and runtime, keeping only persistent database facts and the real Session.
    bootstrap.model_gateway_http_runtime.cache_clear()
    with TestClient(bootstrap.create_app(), base_url="https://testserver") as restarted:
        restarted.cookies.update(journey.admin.cookies)
        assert restarted.get(f"{path}/{receipt['id']}").json() == value
    assert journey.admin.get(f"{BASE}/{deployment['id']}").json() == deployment
    edited = _write(
        journey.admin,
        f"{BASE}/{deployment['id']}",
        {"displayName": "Changed"},
        method="PATCH",
        etag='"v1"',
    ).json()
    stale = journey.admin.get(f"{path}/{receipt['id']}")
    assert stale.json()["state"] == "SUCCEEDED" and stale.json()["currentness"] == "STALE"
    assert "CANDIDATE_CHANGED" in stale.json()["currentnessReasons"]
    assert stale.headers["etag"] != detail.headers["etag"]
    assert (
        _write(
            journey.admin, path, {}, etag='"v1"', status=202, key="check-original-request"
        ).json()["id"]
        == receipt["id"]
    )
    _write(journey.admin, path, {}, etag='"v2"', status=409, key="check-original-request")
    _write(journey.admin, path, {}, etag='"v1"', status=409)
    _, next_receipt = accept(journey, edited)
    assert next_receipt["id"] != receipt["id"]
    # Material rotation preserves the historical success and changes currentness.
    material(root, "material-2")
    manifest.write_text(
        json.dumps(
            {
                "environment": "TEST",
                "connections": [
                    CONNECTION | {"materialVersion": "material-2", "version": "config-2"}
                ],
            }
        )
    )
    assert journey.admin.get(f"{path}/{receipt['id']}").json()["currentness"] == "STALE"
    assert not process_connection_check(next_receipt["id"], dependencies=dependencies)
    assert journey.admin.get(f"{path}/{next_receipt['id']}").json()["state"] == "BLOCKED"
    first = journey.admin.get(path, params={"pageSize": 1}).json()
    second = journey.admin.get(path, params={"pageSize": 1, "cursor": first["nextCursor"]}).json()
    assert first["items"][0]["id"] == next_receipt["id"]
    assert second["items"][0]["id"] == receipt["id"] and second["nextCursor"] is None
    other = candidate(journey, "another-candidate")
    assert (
        journey.admin.get(f"{BASE}/{other['id']}/connection-checks/{receipt['id']}").status_code
        == 404
    )
    assert (
        journey.admin.get(
            f"{BASE}/{other['id']}/connection-checks", params={"cursor": first["nextCursor"]}
        ).status_code
        == 422
    )
    with journey.database.owner.connect() as db:
        audits = db.execute(
            text(
                "SELECT action,reason FROM audit.audit_event "
                "WHERE target_id=:id ORDER BY occurred_at"
            ),
            {"id": receipt["id"]},
        ).all()
        assert [row[0] for row in audits] == [
            "model_connection_check.accepted",
            "model_connection_check.completed",
        ]
        assert '"inputCurrentness": "CURRENT"' in audits[-1][1]
        assert "synthetic-only-secret" not in repr(audits)
    for role, sql in (
        ("model_gateway", "UPDATE model_gateway.connection_check SET state='RUNNING' WHERE id=:id"),
        ("model_gateway_worker", "DELETE FROM model_gateway.connection_check WHERE id=:id"),
        (
            "model_gateway_worker",
            "UPDATE model_gateway.connection_check "
            "SET revision=revision+1,state='QUEUED' WHERE id=:id",
        ),
        (
            "model_gateway_worker",
            "UPDATE model_gateway.deployment SET revision=revision+1 WHERE id=:id",
        ),
    ):
        with pytest.raises(DBAPIError), journey.database.engines[role].begin() as db:
            db.execute(text(sql), {"id": receipt["id"]})


def test_rejections_and_preflight_blocks_make_no_provider_call(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _manifest = configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path = f"{BASE}/{deployment['id']}/connection-checks"
    _grant(journey.admin, journey.member_id, "platform.model.manage")
    _write(journey.member, path, {}, status=403, etag='"v1"')
    assert journey.member.get(path).status_code == 403
    _grant(journey.admin, journey.member_id, "platform.model.read", journey.workspace_id)
    assert journey.member.get(path).status_code == 403
    _grant(journey.admin, journey.member_id, "platform.model.read")
    assert journey.member.get(path).status_code == 200
    for body in (
        {"prompt": "custom"},
        {"apiKey": "sk-synthetic"},
        {"endpoint": "http://127.0.0.1"},
    ):
        rejected = _write(journey.admin, path, body, status=422, etag='"v1"')
        assert "sk-synthetic" not in rejected.text
    assert (
        journey.admin.post(
            path,
            json={},
            headers={
                "Idempotency-Key": "origin-rejected",
                "If-Match": '"v1"',
                "Origin": "https://foreign.invalid",
            },
        ).status_code
        == 403
    )
    _write(journey.admin, path, {}, status=409, etag='"v2"')
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response()

    dependencies = worker(handler)
    missing = candidate(journey, "missing-connection", connectionRef=None)
    missing_path, missing_receipt = accept(journey, missing)
    assert (
        missing_receipt["state"] == "BLOCKED" and missing_receipt["reason"] == "CONNECTION_MISSING"
    )
    disallowed = candidate(journey, "disallowed-model", providerModelId="not-approved")
    _, denied = accept(journey, disallowed)
    assert denied["state"] == "BLOCKED" and denied["reason"] == "MODEL_NOT_ALLOWED"
    _, queued = accept(journey, deployment)
    (root / "trial-key").unlink()
    assert not process_connection_check(queued["id"], dependencies=dependencies)
    assert journey.admin.get(f"{path}/{queued['id']}").json()["reason"] == "MATERIAL_UNAVAILABLE"
    material(root)
    _, queued = accept(journey, deployment)
    _write(journey.admin, f"{BASE}/{deployment['id']}:archive", {"reason": "Retired"}, etag='"v1"')
    assert not process_connection_check(queued["id"], dependencies=dependencies)
    assert journey.admin.get(f"{path}/{queued['id']}").json()["reason"] == "CANDIDATE_ARCHIVED"
    _write(journey.admin, path, {}, status=409, etag='"v2"')
    assert not calls
    assert (
        journey.admin.get(f"{missing_path}/{missing_receipt['id']}").json()["currentness"]
        == "UNVERIFIABLE"
    )


def test_concurrent_admission_single_send_recovery_and_late_fence(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path = f"{BASE}/{deployment['id']}/connection-checks"

    def submit() -> tuple[int, dict[str, Any]]:
        with TestClient(bootstrap.create_app(), base_url="https://testserver") as client:
            client.cookies.update(journey.admin.cookies)
            result = client.post(
                path,
                json={},
                headers={**SAME_ORIGIN, "If-Match": '"v1"', "Idempotency-Key": str(uuid4())},
            )
            return int(result.status_code), result.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(status for status, _ in results) == [202, 409]
    check_id = next(body["id"] for status, body in results if status == 202)
    started, release = Event(), Event()
    calls = []

    def delayed(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        started.set()
        assert release.wait(10)
        return response()

    dependencies = worker(delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process_connection_check, check_id, dependencies=dependencies)
        assert started.wait(10)
        assert not process_connection_check(check_id, dependencies=dependencies)
        running = journey.admin.get(f"{path}/{check_id}").json()
        assert running["state"] == "RUNNING" and running["revision"] == 2
        later = dependencies.common.now() + timedelta(seconds=30)
        recovery = replace(dependencies, common=replace(dependencies.common, now=lambda: later))
        assert recover_expired_checks(dependencies=recovery, limit=10) == 1
        release.set()
        assert first.result(timeout=10)
    value = journey.admin.get(f"{path}/{check_id}").json()
    assert value["state"] == "UNKNOWN" and value["reason"] == "EXECUTION_EXPIRED"
    assert value["revision"] == 3 and value["attempt"] == 1
    assert not process_connection_check(check_id, dependencies=dependencies) and len(calls) == 1
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE target_id=:id"), {"id": check_id}
            ).scalar_one()
            == 2
        )
    _, fresh = accept(journey, deployment)
    assert fresh["id"] != check_id
    assert process_connection_check(fresh["id"], dependencies=dependencies) and len(calls) == 2


def test_connection_slot_and_input_change_during_http_keep_evidence_without_new_send(
    journey: Journey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured(tmp_path, monkeypatch)
    first_candidate = candidate(journey, "slot-first")
    second_candidate = candidate(journey, "slot-second")
    first_path, first_receipt = accept(journey, first_candidate)
    second_path, second_receipt = accept(journey, second_candidate)
    started, release = Event(), Event()
    calls: list[httpx.Request] = []

    def delayed(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        started.set()
        assert release.wait(10)
        return response()

    dependencies = worker(delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        executing = pool.submit(
            process_connection_check, first_receipt["id"], dependencies=dependencies
        )
        assert started.wait(10)
        assert not process_connection_check(second_receipt["id"], dependencies=dependencies)
        blocked = journey.admin.get(f"{second_path}/{second_receipt['id']}").json()
        assert blocked["state"] == "BLOCKED" and blocked["reason"] == "CONNECTION_BUSY"
        _write(
            journey.admin,
            f"{BASE}/{first_candidate['id']}",
            {"displayName": "Edited during probe"},
            method="PATCH",
            etag='"v1"',
        )
        release.set()
        assert executing.result(timeout=10)
    evidence = journey.admin.get(f"{first_path}/{first_receipt['id']}").json()
    assert evidence["state"] == "SUCCEEDED" and evidence["currentness"] == "STALE"
    assert evidence["input"]["deploymentRevision"] == 1
    assert "CANDIDATE_CHANGED" in evidence["currentnessReasons"]
    assert not process_connection_check(second_receipt["id"], dependencies=dependencies)
    assert len(calls) == 1
    with journey.database.owner.connect() as db:
        reason = db.execute(
            text(
                "SELECT reason FROM audit.audit_event WHERE target_id=:id "
                "AND action='model_connection_check.completed'"
            ),
            {"id": first_receipt["id"]},
        ).scalar_one()
        assert json.loads(reason)["inputCurrentness"] == "STALE"


def test_queued_actor_loses_current_qualification_before_any_provider_access(
    journey: Journey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path, receipt = accept(journey, deployment)
    _grant(journey.admin, journey.member_id, "platform.model.read")
    actor_id = journey.admin.get("/api/v1/me").json()["accountId"]
    # Isolated owner fixture simulates a current disabled fact, bypassing the last-admin API guard.
    with journey.database.owner.begin() as db:
        db.execute(
            text("UPDATE identity.account SET status='DISABLED',version=version+1 WHERE id=:id"),
            {"id": actor_id},
        )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response()

    dependencies = worker(handler)
    assert not process_connection_check(receipt["id"], dependencies=dependencies)
    assert not calls
    value = journey.member.get(f"{path}/{receipt['id']}").json()
    assert value["state"] == "BLOCKED" and value["reason"] == "ACTOR_INELIGIBLE"


def test_request_and_terminal_audit_failures_are_atomic_without_resending(
    journey: Journey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path = f"{BASE}/{deployment['id']}/connection-checks"

    class FailingAudit:
        def append_in_transaction(self, db: object, envelope: object) -> None:
            raise RuntimeError("synthetic audit failure")

    runtime = bootstrap.model_gateway_http_runtime()
    broken = replace(runtime, dependencies=replace(runtime.dependencies, audit=FailingAudit()))
    with patch.object(bootstrap, "model_gateway_http_runtime", return_value=broken):
        with TestClient(
            bootstrap.create_app(), base_url="https://testserver", raise_server_exceptions=False
        ) as client:
            client.cookies.update(journey.admin.cookies)
            failure = client.post(
                path,
                json={},
                headers={
                    **SAME_ORIGIN,
                    "If-Match": '"v1"',
                    "Idempotency-Key": "check-audit-rollback",
                },
            )
            assert failure.status_code == 500
    with journey.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM model_gateway.connection_check")).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM model_gateway.idempotency_record "
                    "WHERE idempotency_key='check-audit-rollback'"
                )
            ).scalar_one()
            == 0
        )
    _, receipt = accept(journey, deployment, key="check-audit-rollback")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response()

    dependencies = worker(handler)
    broken_worker = replace(dependencies, common=replace(dependencies.common, audit=FailingAudit()))
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        process_connection_check(receipt["id"], dependencies=broken_worker)
    assert journey.admin.get(f"{path}/{receipt['id']}").json()["state"] == "RUNNING"
    assert not process_connection_check(receipt["id"], dependencies=dependencies)
    later = dependencies.common.now() + timedelta(seconds=30)
    recovery = replace(dependencies, common=replace(dependencies.common, now=lambda: later))
    assert recover_expired_checks(dependencies=recovery, limit=10) == 1
    assert len(calls) == 1
    assert journey.admin.get(f"{path}/{receipt['id']}").json()["state"] == "UNKNOWN"
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE target_id=:id"),
                {"id": receipt["id"]},
            ).scalar_one()
            == 2
        )
