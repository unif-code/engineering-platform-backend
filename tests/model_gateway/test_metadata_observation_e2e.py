"""CI PostgreSQL only: default Session, real owner transactions and injected synthetic HTTP."""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from io import StringIO
from pathlib import Path
from threading import Event
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.bootstrap.model_gateway_worker import model_metadata_worker_dependencies
from control_plane.app.modules.model_gateway import (
    process_connection_check,
    process_metadata_observation,
    recover_expired_metadata_observations,
)
from control_plane.app.modules.model_gateway.adapters.metadata import HttpxModelMetadata
from control_plane.app.modules.model_gateway.application.metadata_worker import (
    ModelMetadataWorkerDependencies,
)
from control_plane.tools import model_gateway_worker
from tests.model_gateway.test_connection_check_e2e import accept as accept_check
from tests.model_gateway.test_connection_check_e2e import candidate, configured
from tests.model_gateway.test_connection_check_e2e import worker as check_worker
from tests.model_gateway.test_metadata_adapter import RESPONSE, SOURCE
from tests.model_gateway.test_probe import public_dns, response
from tests.source_control.test_v06_production_e2e import Journey, _grant, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database
from tests.test_e2e_access_governance import SAME_ORIGIN

pytestmark = pytest.mark.integration


def metadata_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    configured(tmp_path, monkeypatch)
    path = tmp_path / "metadata-sources.json"
    path.write_text(json.dumps({"schemaVersion": 1, "environment": "TEST", "sources": [SOURCE]}))
    monkeypatch.setenv("MODEL_GATEWAY_METADATA_SOURCES_PATH", str(path))
    bootstrap.model_gateway_http_runtime.cache_clear()
    return path


def accept_metadata(
    journey: Journey, deployment: dict[str, Any], *, key: str | None = None
) -> tuple[str, dict[str, Any], dict[str, str]]:
    path = f"/api/v1/admin/model-deployments/{deployment['id']}/metadata-observations"
    listed = journey.admin.get(path)
    assert listed.status_code == 200, listed.text
    body = {"sourceFingerprint": listed.json()["admission"]["source"]["fingerprint"]}
    receipt = _write(
        journey.admin, path, body, status=202, etag=f'"v{deployment["revision"]}"', key=key
    )
    return path, receipt.json(), body


def metadata_worker(handler: Any) -> ModelMetadataWorkerDependencies:
    return model_metadata_worker_dependencies(
        adapter_factory=lambda secrets: HttpxModelMetadata(
            secrets,
            transport=httpx.MockTransport(handler),
            resolver=public_dns,
        )
    )


def test_default_metadata_session_worker_replay_readonly_history_and_roles(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_file = metadata_configured(tmp_path, monkeypatch)
    deployment = candidate(journey, declaredContextWindow=8192, declaredMaxOutputTokens=1024)
    candidate_path = f"/api/v1/admin/model-deployments/{deployment['id']}"
    before = journey.admin.get(candidate_path).json()
    key = "metadata-original-admission"
    path, receipt, body = accept_metadata(journey, deployment, key=key)
    assert receipt["state"] == "QUEUED"
    calls: list[httpx.Request] = []
    provider_status = 200

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/api/v1/models"
        return response(RESPONSE, status=provider_status)

    dependencies = metadata_worker(handler)
    output = StringIO()
    with redirect_stdout(output):
        assert (
            model_gateway_worker.main(
                ["--kind", "metadata-observations", "--limit", "1"],
                metadata_dependencies_provider=lambda: dependencies,
            )
            == 0
        )
    assert json.loads(output.getvalue())["attempted"] == 1 and len(calls) == 1
    assert not process_metadata_observation(receipt["id"], dependencies=dependencies)
    result = journey.admin.get(path + "/" + receipt["id"])
    assert result.status_code == 200, result.text
    observed = result.json()
    assert observed["state"] == "REPORTED" and observed["report"]["specificationVersion"] is None
    assert observed["contextWindowComparison"]["comparison"] == "EQUAL"
    assert observed["maxOutputTokensComparison"]["comparison"] == "DIFFERENT"
    assert observed["report"]["maxInputTokens"]["state"] == "NULL"
    assert journey.admin.get(path + "/" + receipt["id"]).json() == observed and len(calls) == 1
    assert journey.admin.get(candidate_path).json() == before
    assert "secret-ref:" not in result.text and "synthetic-only-secret" not in result.text
    assert journey.member.get(path).status_code == 403
    _grant(journey.admin, journey.member_id, "platform.model.read")
    assert journey.member.get(path).status_code == 200
    _grant(journey.admin, journey.member_id, "platform.model.manage")
    assert (
        journey.member.post(
            path,
            json=body,
            headers={
                **SAME_ORIGIN,
                "If-Match": '"v1"',
                "Idempotency-Key": "metadata-member-denied",
            },
        ).status_code
        == 403
    )

    source_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "environment": "TEST",
                "sources": [SOURCE | {"version": "source-2"}],
            }
        )
    )
    replay = _write(journey.admin, path, body, status=202, etag='"v1"', key=key)
    assert replay.json() == receipt and len(calls) == 1
    changed = journey.admin.get(path + "/" + receipt["id"])
    assert (
        changed.json()["resultHash"] == observed["resultHash"]
        and changed.json()["applicability"] == "STALE"
    )
    assert changed.headers["etag"] != result.headers["etag"]
    _, second, new_body = accept_metadata(journey, deployment)
    assert new_body != body
    assert (
        _write(journey.admin, path, new_body, status=409, etag='"v1"', key=key).status_code == 409
    )
    provider_status = 403
    assert process_metadata_observation(second["id"], dependencies=dependencies)
    failure = journey.admin.get(path + "/" + second["id"])
    assert failure.status_code == 200 and failure.json()["reason"] == "PROVIDER_REJECTED"
    assert journey.admin.get(path + "/" + receipt["id"]).json()["report"] == observed["report"]
    page = journey.admin.get(path, params={"pageSize": 1}).json()
    assert len(page["items"]) == 1 and page["nextCursor"]
    tail = journey.admin.get(path, params={"pageSize": 1, "cursor": page["nextCursor"]}).json()
    assert len(tail["items"]) == 1 and tail["nextCursor"] is None
    assert len(calls) == 2 and journey.admin.get(candidate_path).json() == before
    for role in ("model_gateway", "model_gateway_worker"):
        for statement in (
            "UPDATE model_gateway.metadata_observation SET revision=revision+1 WHERE id=:id",
            "DELETE FROM model_gateway.metadata_observation WHERE id=:id",
        ):
            with pytest.raises(DBAPIError), journey.database.engines[role].begin() as db:
                db.execute(text(statement), {"id": receipt["id"]})


@pytest.mark.parametrize("first", ["metadata", "check"])
def test_real_cross_type_connection_admission_in_both_directions(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    metadata_configured(tmp_path, monkeypatch)
    meta_candidate = candidate(journey, key="metadata-slot")
    check_candidate = candidate(journey, key="check-slot")
    meta_path, meta, _ = accept_metadata(journey, meta_candidate)
    check_path, check = accept_check(journey, check_candidate)
    entered, release = Event(), Event()
    calls: list[str] = []

    def metadata_http(_request: httpx.Request) -> httpx.Response:
        calls.append("metadata")
        assert first == "metadata"
        entered.set()
        assert release.wait(10)
        return response(RESPONSE)

    def check_http(_request: httpx.Request) -> httpx.Response:
        calls.append("check")
        assert first == "check"
        entered.set()
        assert release.wait(10)
        return response()

    meta_deps, check_deps = metadata_worker(metadata_http), check_worker(check_http)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = (
            pool.submit(process_metadata_observation, meta["id"], dependencies=meta_deps)
            if first == "metadata"
            else pool.submit(process_connection_check, check["id"], dependencies=check_deps)
        )
        try:
            assert entered.wait(10)
            if first == "metadata":
                assert not process_connection_check(check["id"], dependencies=check_deps)
                blocked = journey.admin.get(check_path + "/" + check["id"]).json()
            else:
                assert not process_metadata_observation(meta["id"], dependencies=meta_deps)
                blocked = journey.admin.get(meta_path + "/" + meta["id"]).json()
            assert blocked["state"] == "BLOCKED" and blocked["reason"] == "CONNECTION_BUSY"
        finally:
            release.set()
        assert future.result(timeout=10)
    assert calls == [first]


def test_real_metadata_lease_recovery_does_not_send_and_preserves_original_receipt(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata_configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    key = "metadata-expired-original"
    path, receipt, body = accept_metadata(journey, deployment, key=key)
    now = bootstrap.model_gateway_http_runtime().dependencies.now()
    for missing in ("token", "started", "deadline"):
        values = {
            "token": str(uuid4()),
            "started": now,
            "deadline": now + timedelta(seconds=25),
            "id": receipt["id"],
        }
        values[missing] = None
        with pytest.raises(DBAPIError) as caught:
            with journey.database.engines["model_gateway_worker"].begin() as db:
                db.execute(
                    text("""
                    UPDATE model_gateway.metadata_observation
                    SET state='RUNNING',revision=revision+1,attempt=1,
                        execution_token=:token,started_at=:started,deadline_at=:deadline
                    WHERE id=:id
                """),
                    values,
                )
        assert getattr(caught.value.orig, "sqlstate", None) == "23514"
        assert journey.admin.get(path + "/" + receipt["id"]).json()["state"] == "QUEUED"
    # The same role and SQL fields must still admit a complete live fence.
    with journey.database.engines["model_gateway_worker"].begin() as db:
        db.execute(
            text("""
            UPDATE model_gateway.metadata_observation SET state='RUNNING',revision=revision+1,
                attempt=1,execution_token=:token,started_at=:started,deadline_at=:deadline
            WHERE id=:id
        """),
            {
                "token": str(uuid4()),
                "started": now,
                "deadline": now + timedelta(seconds=25),
                "id": receipt["id"],
            },
        )

    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("expired observation must not dispatch")

    original = metadata_worker(forbidden)
    deps = replace(
        original, common=replace(original.common, now=lambda: now + timedelta(seconds=26))
    )
    assert recover_expired_metadata_observations(dependencies=deps, limit=1) == 1
    assert not process_metadata_observation(receipt["id"], dependencies=deps)
    assert journey.admin.get(path + "/" + receipt["id"]).json()["state"] == "UNKNOWN"
    assert _write(journey.admin, path, body, status=202, etag='"v1"', key=key).json() == receipt


def test_real_metadata_admission_audit_failure_rolls_back_job_and_receipt(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata_configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path = f"/api/v1/admin/model-deployments/{deployment['id']}/metadata-observations"
    source = journey.admin.get(path).json()["admission"]["source"]

    class FailingAudit:
        def append_in_transaction(self, db: object, envelope: object) -> None:
            raise RuntimeError("synthetic audit failure")

    runtime = bootstrap.model_gateway_http_runtime()
    broken = replace(runtime, dependencies=replace(runtime.dependencies, audit=FailingAudit()))
    with (
        patch.object(bootstrap, "model_gateway_http_runtime", return_value=broken),
        TestClient(
            bootstrap.create_app(), base_url="https://testserver", raise_server_exceptions=False
        ) as client,
    ):
        client.cookies.update(journey.admin.cookies)
        response_value = client.post(
            path,
            json={"sourceFingerprint": source["fingerprint"]},
            headers={
                **SAME_ORIGIN,
                "If-Match": '"v1"',
                "Idempotency-Key": "metadata-audit-failure",
            },
        )
        assert response_value.status_code == 500
    with journey.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM model_gateway.metadata_observation")).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM model_gateway.idempotency_record "
                    "WHERE idempotency_key='metadata-audit-failure'"
                )
            ).scalar_one()
            == 0
        )


def test_real_claim_commit_recovery_fences_provider_dispatch(
    journey: Journey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    path, receipt, _ = accept_metadata(journey, deployment)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(RESPONSE)

    original = metadata_worker(handler)
    clock = {"now": original.common.now()}
    dependencies = replace(original, common=replace(original.common, now=lambda: clock["now"]))
    engine = original.engine
    intercepted = False

    class CommitHook:
        def connect(self) -> Connection:
            return engine.connect()

        @contextmanager
        def begin(self) -> Iterator[Connection]:
            nonlocal intercepted
            with engine.begin() as db:
                yield db
            if not intercepted:
                with engine.connect() as db:
                    state = db.execute(
                        text("SELECT state FROM model_gateway.metadata_observation WHERE id=:id"),
                        {"id": receipt["id"]},
                    ).scalar_one()
                if state == "RUNNING":
                    intercepted = True
                    clock["now"] += timedelta(seconds=26)
                    assert (
                        recover_expired_metadata_observations(
                            dependencies=dependencies,
                            limit=1,
                        )
                        == 1
                    )

    guarded = replace(dependencies, engine=cast(Engine, CommitHook()))
    assert not process_metadata_observation(receipt["id"], dependencies=guarded)
    assert intercepted and calls == []
    result = journey.admin.get(path + "/" + receipt["id"])
    assert result.status_code == 200 and result.json()["state"] == "UNKNOWN"
    assert result.json()["reason"] == "EXECUTION_EXPIRED"
    assert not process_metadata_observation(receipt["id"], dependencies=dependencies)
