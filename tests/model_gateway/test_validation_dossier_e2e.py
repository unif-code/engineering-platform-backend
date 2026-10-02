from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.model_gateway import process_connection_check
from control_plane.app.modules.model_gateway.adapters.dossiers import (
    SqlAlchemyValidationDossierRepository,
)
from tests.model_gateway.test_connection_check_e2e import (
    BASE,
    accept,
    candidate,
    configured,
    worker,
)
from tests.model_gateway.test_probe import RESPONSE, response
from tests.source_control.test_v06_production_e2e import Journey, _grant, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database
from tests.test_e2e_access_governance import SAME_ORIGIN

pytestmark = pytest.mark.integration


def test_dossier_default_session_snapshot_replay_projection_restart_and_roles(
    journey: Journey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    _, check = accept(journey, deployment)
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(RESPONSE)

    assert process_connection_check(check["id"], dependencies=worker(provider))
    path = f"{BASE}/{deployment['id']}/validation-dossiers"
    runtime = bootstrap.model_gateway_http_runtime()
    now = datetime.now(UTC)
    body = {
        "materials": [
            {
                "category": "PRICING",
                "title": "Declared tariff source",
                "sourceReference": "https://example.org/model%20price?tracking=discard#v1",
                "declaredContentSha256": "a" * 64,
                "expiresAt": (now + timedelta(days=1)).isoformat(),
            }
        ],
        "checkIds": [check["id"]],
    }
    with patch.object(
        httpx.HTTPTransport, "handle_request", side_effect=AssertionError("No dossier network")
    ):
        receipt = _write(
            journey.admin, path, body, etag='"v1"', key="dossier-original", status=201
        ).json()
        assert (
            _write(
                journey.admin, path, body, etag='"v1"', key="dossier-original", status=201
            ).json()
            == receipt
        )
        detail = journey.admin.get(f"{path}/{receipt['id']}")
        assert detail.status_code == 200
    value = detail.json()
    snapshot = value["snapshot"]
    assert snapshot["snapshotHash"] == receipt["snapshotHash"]
    assert snapshot["materials"][0]["provenance"] == "DECLARED"
    assert snapshot["materials"][0]["sourceReference"] == "https://example.org/model%20price"
    assert snapshot["checks"][0]["resultSummary"]["state"] == "SUCCEEDED"
    assert value["currentness"] == "CURRENT" and detail.headers["etag"].startswith('"dossier-')
    assert len(value["materialCoverage"]) == 8 and len(value["checkCoverage"]) == 5
    assert journey.admin.get(f"{BASE}/{deployment['id']}").json() == deployment
    assert len(calls) == 1
    _write(
        journey.admin,
        path,
        {"materials": [body["materials"][0] | {"title": "Changed"}]},
        etag='"v1"',
        key="dossier-original",
        status=409,
    )
    _write(journey.admin, path, body, etag='"v2"', key="dossier-original", status=409)
    _write(
        journey.admin,
        path,
        body
        | {
            "materials": [
                body["materials"][0]
                | {
                    "sourceReference": "https://example.org/model%20price?tracking=changed#v2",
                }
            ]
        },
        etag='"v1"',
        key="dossier-original",
        status=409,
    )
    later = replace(
        runtime, dependencies=replace(runtime.dependencies, now=lambda: now + timedelta(days=2))
    )
    with patch.object(bootstrap, "model_gateway_http_runtime", return_value=later):
        with TestClient(bootstrap.create_app(), base_url="https://testserver") as restarted:
            restarted.cookies.update(journey.admin.cookies)
            expired = restarted.get(f"{path}/{receipt['id']}")
    assert expired.json()["snapshot"] == snapshot
    assert expired.json()["materialStatuses"][0]["expiration"] == "EXPIRED"
    assert expired.headers["etag"] != detail.headers["etag"]
    second = _write(
        journey.admin,
        path,
        {
            "materials": [
                {
                    "category": "HEALTH",
                    "title": "Declared",
                    "sourceReference": "urn:provider:health",
                }
            ]
        },
        etag='"v1"',
        status=201,
    ).json()
    first_page = journey.admin.get(path, params={"pageSize": 1}).json()
    second_page = journey.admin.get(
        path, params={"pageSize": 1, "cursor": first_page["nextCursor"]}
    ).json()
    assert {first_page["items"][0]["id"], second_page["items"][0]["id"]} == {
        receipt["id"],
        second["id"],
    }
    assert second_page["nextCursor"] is None
    _write(
        journey.admin,
        f"{BASE}/{deployment['id']}",
        {"displayName": "New revision"},
        method="PATCH",
        etag='"v1"',
    )
    changed = journey.admin.get(f"{path}/{receipt['id']}")
    assert changed.json()["snapshot"] == snapshot and changed.json()["currentness"] == "STALE"
    assert changed.headers["etag"] != detail.headers["etag"]
    _write(journey.admin, f"{BASE}/{deployment['id']}:archive", {"reason": "Retired"}, etag='"v2"')
    assert (
        _write(journey.admin, path, body, etag='"v1"', key="dossier-original", status=201).json()
        == receipt
    )
    _write(journey.admin, path, body, etag='"v3"', status=409)
    assert journey.admin.get(f"{path}/{receipt['id']}").json()["snapshot"] == snapshot
    with journey.database.owner.connect() as db:
        audits = (
            db.execute(
                text("SELECT reason FROM audit.audit_event WHERE target_id=:id"),
                {"id": receipt["id"]},
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1 and "https://" not in audits[0]
        assert db.execute(
            text(
                "SELECT snapshot_hash=encode(sha256(convert_to(snapshot_text,'UTF8')),'hex') "
                "FROM model_gateway.validation_dossier WHERE id=:id"
            ),
            {"id": receipt["id"]},
        ).scalar_one()
    for role, sql in (
        (
            "model_gateway",
            "UPDATE model_gateway.validation_dossier SET snapshot_hash=repeat('b',64) WHERE id=:id",
        ),
        ("model_gateway", "DELETE FROM model_gateway.validation_dossier WHERE id=:id"),
        ("model_gateway_worker", "SELECT * FROM model_gateway.validation_dossier WHERE id=:id"),
    ):
        with pytest.raises(DBAPIError), journey.database.engines[role].begin() as db:
            db.execute(text(sql), {"id": receipt["id"]})
    with pytest.raises(DBAPIError), journey.database.engines["model_gateway"].begin() as db:
        db.execute(
            text("UPDATE model_gateway.connection_check SET id=id WHERE id=:id"),
            {"id": check["id"]},
        )


def test_dossier_reference_guards_and_current_authorization(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(tmp_path, monkeypatch)
    deployment = candidate(journey)
    other = candidate(journey, "other-model")
    path = f"{BASE}/{deployment['id']}/validation-dossiers"
    _, active = accept(journey, deployment)
    body = {"checkIds": [active["id"]]}
    assert (
        _write(journey.admin, path, body, etag='"v1"', status=409).json()["code"]
        == "MODEL_DOSSIER_CHECK_NOT_TERMINAL"
    )
    _write(
        journey.admin, f"{BASE}/{other['id']}/validation-dossiers", body, etag='"v1"', status=404
    )
    assert process_connection_check(active["id"], dependencies=worker(lambda _: response(RESPONSE)))
    _, next_check = accept(journey, deployment)
    assert process_connection_check(
        next_check["id"], dependencies=worker(lambda _: response(RESPONSE))
    )
    _write(
        journey.admin, path, {"checkIds": [active["id"], next_check["id"]]}, etag='"v1"', status=409
    )
    receipt = _write(
        journey.admin, path, body, etag='"v1"', key="authorized-dossier", status=201
    ).json()
    assert journey.member.get(path).status_code == 403
    _grant(journey.admin, journey.member_id, "platform.model.read", journey.workspace_id)
    assert journey.member.get(path).status_code == 403
    _grant(journey.admin, journey.member_id, "platform.model.read")
    _grant(journey.admin, journey.member_id, "platform.model.manage")
    assert journey.member.get(path).status_code == 200
    _write(journey.member, path, body, etag='"v1"', key="authorized-dossier", status=403)
    assert (
        journey.admin.get(f"{BASE}/{other['id']}/validation-dossiers/{receipt['id']}").status_code
        == 404
    )
    _write(
        journey.admin,
        f"{BASE}/{deployment['id']}",
        {"description": "Revised"},
        method="PATCH",
        etag='"v1"',
    )
    assert (
        _write(journey.admin, path, body, etag='"v2"', status=409).json()["code"]
        == "MODEL_DOSSIER_CHECK_INPUT_CHANGED"
    )
    assert journey.admin.get(path, params={"cursor": "malformed"}).status_code == 422


def test_dossier_audit_failure_rolls_back_snapshot_and_receipt(journey: Journey) -> None:
    deployment = candidate(journey)
    path = f"{BASE}/{deployment['id']}/validation-dossiers"
    body = {
        "materials": [
            {
                "category": "MODEL_IDENTITY",
                "title": "Source",
                "sourceReference": "urn:provider:model",
            }
        ]
    }

    class FailingAudit:
        def append_in_transaction(self, db: Any, envelope: Any) -> None:
            raise RuntimeError("synthetic audit failure")

    runtime = bootstrap.model_gateway_http_runtime()
    broken = replace(runtime, dependencies=replace(runtime.dependencies, audit=FailingAudit()))
    with patch.object(bootstrap, "model_gateway_http_runtime", return_value=broken):
        with TestClient(
            bootstrap.create_app(), base_url="https://testserver", raise_server_exceptions=False
        ) as client:
            client.cookies.update(journey.admin.cookies)
            failed = client.post(
                path,
                json=body,
                headers={**SAME_ORIGIN, "If-Match": '"v1"', "Idempotency-Key": "dossier-rollback"},
            )
            assert failed.status_code == 500
    with journey.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM model_gateway.validation_dossier")).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM model_gateway.idempotency_record "
                    "WHERE idempotency_key='dossier-rollback'"
                )
            ).scalar_one()
            == 0
        )
    _write(journey.admin, path, body, etag='"v1"', key="dossier-rollback", status=201)


def test_dossiers_bind_failed_unknown_and_blocked_checks_truthfully(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(tmp_path, monkeypatch)
    for state in ("FAILED", "UNKNOWN", "BLOCKED"):
        deployment = candidate(
            journey,
            f"terminal-{state.lower()}",
            **({"connectionRef": None} if state == "BLOCKED" else {}),
        )
        check_path, receipt = accept(journey, deployment)
        if state != "BLOCKED":

            def provider(request: httpx.Request, result_state: str = state) -> httpx.Response:
                if result_state == "UNKNOWN":
                    raise httpx.ReadError("synthetic interrupted response", request=request)
                return httpx.Response(200, json={"invalid": True})

            process_connection_check(receipt["id"], dependencies=worker(provider))
        check = journey.admin.get(f"{check_path}/{receipt['id']}").json()
        assert check["state"] == state
        path = f"{BASE}/{deployment['id']}/validation-dossiers"
        dossier = _write(
            journey.admin, path, {"checkIds": [receipt["id"]]}, etag='"v1"', status=201
        ).json()
        detail = journey.admin.get(f"{path}/{dossier['id']}").json()
        assert detail["snapshot"]["checks"][0]["resultSummary"]["state"] == state
        assert all(item["status"] == "MISSING" for item in detail["materialCoverage"])


@pytest.mark.parametrize("archive", [False, True])
def test_dossier_registration_serializes_candidate_edit_or_archive(
    journey: Journey, monkeypatch: pytest.MonkeyPatch, archive: bool
) -> None:
    deployment = candidate(journey)
    path = f"{BASE}/{deployment['id']}"
    body = {
        "materials": [
            {
                "category": "MODEL_IDENTITY",
                "title": "Source",
                "sourceReference": "urn:provider:model",
            }
        ]
    }
    locked, release = Event(), Event()
    insert = SqlAlchemyValidationDossierRepository.insert_dossier

    def paused_insert(repository: SqlAlchemyValidationDossierRepository, value: Any) -> None:
        locked.set()
        assert release.wait(10)
        insert(repository, value)

    monkeypatch.setattr(SqlAlchemyValidationDossierRepository, "insert_dossier", paused_insert)
    with ThreadPoolExecutor(max_workers=2) as pool:
        create = pool.submit(
            _write, journey.admin, path + "/validation-dossiers", body, etag='"v1"', status=201
        )
        try:
            assert locked.wait(10)
            mutation = pool.submit(
                _write,
                journey.admin,
                path + (":archive" if archive else ""),
                {"reason": "Retired"} if archive else {"displayName": "Changed"},
                method="POST" if archive else "PATCH",
                etag='"v1"',
            )
            with pytest.raises(TimeoutError):
                mutation.result(timeout=0.2)
        finally:
            release.set()
        receipt = create.result(timeout=10).json()
        assert mutation.result(timeout=10).json()["revision"] == 2
    detail = journey.admin.get(f"{path}/validation-dossiers/{receipt['id']}").json()
    assert detail["snapshot"]["candidateRevision"] == 1 and detail["currentness"] == "STALE"
