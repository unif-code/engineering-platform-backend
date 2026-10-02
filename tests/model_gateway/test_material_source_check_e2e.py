import hashlib
import json
import os
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
from control_plane.app.modules.model_gateway.adapters.material_sources import (
    FileModelMaterialSource,
)
from control_plane.app.modules.model_gateway.domain.source_checks import SourceInspection
from tests.model_gateway.test_connection_check_e2e import BASE, candidate
from tests.model_gateway.test_material_source_copy import REFERENCE, source
from tests.source_control.test_v06_production_e2e import Journey, _grant, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database
from tests.test_e2e_access_governance import SAME_ORIGIN

pytestmark = pytest.mark.integration


def configured(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], dict[str, Any], Path, Path, dict[str, Any], str]:
    _port, root, manifest, entry = source(tmp_path, b"first")
    monkeypatch.setenv("MODEL_GATEWAY_ENVIRONMENT", "TEST")
    monkeypatch.setenv("MODEL_GATEWAY_MATERIAL_SOURCES_PATH", str(manifest))
    monkeypatch.setenv("MODEL_GATEWAY_MATERIAL_SOURCES_ROOT", str(root))
    bootstrap.model_gateway_http_runtime.cache_clear()
    deployment = candidate(journey)
    material = {
        "category": "CONTEXT_LIMITS",
        "title": "Declared documentation",
        "sourceReference": REFERENCE,
        "externalVersion": "2026-10",
        "declaredContentSha256": entry["copySha256"],
        "expiresAt": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    dossier = _write(
        journey.admin,
        f"{BASE}/{deployment['id']}/validation-dossiers",
        {"materials": [material, material | {"declaredContentSha256": None, "expiresAt": None}]},
        etag='"v1"',
        status=201,
    ).json()
    path = f"{BASE}/{deployment['id']}/validation-dossiers/{dossier['id']}/source-checks"
    return deployment, dossier, root, manifest, entry, path


def test_source_check_default_journey_replay_restart_raw_remeasurement_and_roles(
    journey: Journey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, dossier, root, manifest, entry, path = configured(journey, tmp_path, monkeypatch)
    calls: list[tuple[str, str | None]] = []
    inspect = FileModelMaterialSource.inspect

    def counted(
        port: FileModelMaterialSource, reference: str, version: str | None
    ) -> SourceInspection:
        calls.append((reference, version))
        return inspect(port, reference, version)

    monkeypatch.setattr(FileModelMaterialSource, "inspect", counted)
    with patch.object(
        httpx.HTTPTransport,
        "handle_request",
        side_effect=AssertionError("No source/Provider network"),
    ):
        receipt = _write(
            journey.admin,
            path,
            {"materialIndex": 0},
            etag='"v1"',
            key="source-original",
            status=201,
        ).json()
        assert "result" not in receipt
        assert len(calls) == 1
        assert (
            _write(
                journey.admin,
                path,
                {"materialIndex": 0},
                etag='"v1"',
                key="source-original",
                status=201,
            ).json()
            == receipt
        )
        assert len(calls) == 1
        detail = journey.admin.get(f"{path}/{receipt['id']}")
        assert detail.status_code == 200 and len(calls) == 2
    value = detail.json()
    snapshot = value["snapshot"]
    assert snapshot["result"] == "MATCHED"
    assert snapshot["observedSha256"] == entry["copySha256"] and snapshot["observedBytes"] == 5
    assert snapshot["dossierSnapshotHash"] == dossier["snapshotHash"]
    assert value["currentness"] == "CURRENT" and value["materialExpiration"] == "NOT_EXPIRED"
    assert str(root) not in detail.text and "relativePath" not in detail.text
    parent_path = f"{BASE}/{deployment['id']}/validation-dossiers/{dossier['id']}"
    original_dossier = journey.admin.get(parent_path).json()["snapshot"]
    assert original_dossier["materials"][0]["provenance"] == "DECLARED"
    assert journey.admin.get(f"{BASE}/{deployment['id']}").json() == deployment
    _write(
        journey.admin, path, {"materialIndex": 1}, etag='"v1"', key="source-original", status=409
    )
    _write(
        journey.admin, path, {"materialIndex": 0}, etag='"v2"', key="source-original", status=409
    )
    reads = len(calls)
    missing = _write(journey.admin, path, {"materialIndex": 1}, etag='"v1"', status=201).json()
    assert len(calls) == reads
    no_declaration = journey.admin.get(f"{path}/{missing['id']}").json()
    assert len(calls) == reads
    assert no_declaration["snapshot"]["reason"] == "DECLARED_HASH_MISSING"
    assert (
        no_declaration["snapshot"]["observedSha256"]
        is no_declaration["snapshot"]["observedBytes"]
        is None
    )
    assert no_declaration["materialExpiration"] == "NOT_DECLARED"
    assert no_declaration["currentness"] == "UNVERIFIABLE"
    first_page = journey.admin.get(path, params={"pageSize": 1}).json()
    next_page = journey.admin.get(
        path, params={"pageSize": 1, "cursor": first_page["nextCursor"]}
    ).json()
    assert {first_page["items"][0]["id"], next_page["items"][0]["id"]} == {
        receipt["id"],
        missing["id"],
    }
    assert len(calls) == reads
    bootstrap.model_gateway_http_runtime.cache_clear()
    with TestClient(bootstrap.create_app(), base_url="https://testserver") as restarted:
        restarted.cookies.update(journey.admin.cookies)
        assert restarted.get(f"{path}/{receipt['id']}").json()["snapshot"] == snapshot
    leaf = root / "model.bin"
    before = leaf.stat()
    other = root / "replacement.bin"
    other.write_bytes(b"other")
    os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
    other.replace(leaf)
    reads = len(calls)
    assert (
        _write(
            journey.admin,
            path,
            {"materialIndex": 0},
            etag='"v1"',
            key="source-original",
            status=201,
        ).json()
        == receipt
    )
    assert len(calls) == reads
    changed = journey.admin.get(f"{path}/{receipt['id']}")
    assert changed.json()["snapshot"] == snapshot
    assert changed.json()["currentness"] == "STALE"
    assert "SOURCE_CONTENT_CHANGED" in changed.json()["currentnessReasons"]
    assert changed.headers["etag"] != detail.headers["etag"]
    integrity = _write(journey.admin, path, {"materialIndex": 0}, etag='"v1"', status=201).json()
    blocked = journey.admin.get(f"{path}/{integrity['id']}").json()
    assert blocked["snapshot"]["result"] == "BLOCKED"
    assert blocked["snapshot"]["reason"] == "APPROVED_COPY_HASH_MISMATCH"
    assert blocked["snapshot"]["observedSha256"] == hashlib.sha256(b"other").hexdigest()
    assert blocked["currentness"] == "UNVERIFIABLE"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "environment": "TEST",
                "sources": [
                    entry | {"copySha256": hashlib.sha256(b"other").hexdigest()},
                ],
            }
        )
    )
    mismatch = _write(journey.admin, path, {"materialIndex": 0}, etag='"v1"', status=201).json()
    mismatch_detail = journey.admin.get(f"{path}/{mismatch['id']}").json()
    assert mismatch_detail["snapshot"]["result"] == "MISMATCH"
    assert mismatch_detail["currentness"] == "CURRENT"
    runtime = bootstrap.model_gateway_http_runtime()
    later = replace(
        runtime,
        dependencies=replace(
            runtime.dependencies, now=lambda: datetime.now(UTC) + timedelta(days=2)
        ),
    )
    with patch.object(bootstrap, "model_gateway_http_runtime", return_value=later):
        with TestClient(bootstrap.create_app(), base_url="https://testserver") as client:
            client.cookies.update(journey.admin.cookies)
            expired = client.get(f"{path}/{mismatch['id']}").json()
    assert expired["snapshot"] == mismatch_detail["snapshot"]
    assert expired["materialExpiration"] == "EXPIRED"
    assert journey.admin.get(parent_path).json()["snapshot"] == original_dossier
    with journey.database.owner.connect() as db:
        audits = (
            db.execute(
                text("SELECT reason FROM audit.audit_event WHERE target_id=:id"),
                {"id": receipt["id"]},
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1 and str(root) not in audits[0] and "https://" not in audits[0]
        assert db.execute(
            text(
                "SELECT snapshot_hash=encode(sha256(convert_to(snapshot_text,'UTF8')),'hex') "
                "FROM model_gateway.material_source_check WHERE id=:id"
            ),
            {"id": receipt["id"]},
        ).scalar_one()
    for role, sql in (
        (
            "model_gateway",
            "UPDATE model_gateway.material_source_check "
            "SET snapshot_hash=repeat('a',64) WHERE id=:id",
        ),
        ("model_gateway", "DELETE FROM model_gateway.material_source_check WHERE id=:id"),
        ("model_gateway_worker", "SELECT * FROM model_gateway.material_source_check WHERE id=:id"),
    ):
        with pytest.raises(DBAPIError), journey.database.engines[role].begin() as db:
            db.execute(text(sql), {"id": receipt["id"]})


def test_source_check_admission_authorization_and_unconfigured_isolation(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment, dossier, _, _, _, path = configured(journey, tmp_path, monkeypatch)
    _write(journey.admin, path, {"materialIndex": 31}, etag='"v1"', status=404)
    _write(
        journey.admin,
        path,
        {"materialIndex": 0, "relativePath": "/etc/passwd"},
        etag='"v1"',
        status=422,
    )
    other = candidate(journey, "other-source-model")
    foreign = f"{BASE}/{other['id']}/validation-dossiers/{dossier['id']}/source-checks"
    _write(journey.admin, foreign, {"materialIndex": 0}, etag='"v1"', status=404)
    assert journey.member.get(path).status_code == 403
    _grant(journey.admin, journey.member_id, "platform.model.read", journey.workspace_id)
    assert journey.member.get(path).status_code == 403
    _grant(journey.admin, journey.member_id, "platform.model.read")
    _grant(journey.admin, journey.member_id, "platform.model.manage")
    assert journey.member.get(path).status_code == 200
    _write(journey.member, path, {"materialIndex": 0}, etag='"v1"', status=403)
    monkeypatch.delenv("MODEL_GATEWAY_MATERIAL_SOURCES_PATH")
    monkeypatch.delenv("MODEL_GATEWAY_MATERIAL_SOURCES_ROOT")
    bootstrap.model_gateway_http_runtime.cache_clear()
    blocked = _write(
        journey.admin,
        path,
        {"materialIndex": 0},
        etag='"v1"',
        key="source-unconfigured",
        status=201,
    ).json()
    detail = journey.admin.get(f"{path}/{blocked['id']}").json()
    assert detail["snapshot"]["reason"] == "SOURCE_DIRECTORY_UNCONFIGURED"
    assert detail["snapshot"]["observedBytes"] is None
    assert (
        journey.admin.get(
            f"{BASE}/{deployment['id']}/validation-dossiers/{dossier['id']}"
        ).status_code
        == 200
    )
    assert journey.admin.get(BASE).status_code == 200
    _write(
        journey.admin,
        f"{BASE}/{deployment['id']}/connection-checks",
        {"checkKind": "BASIC_TEXT"},
        etag='"v1"',
        status=202,
    )
    _write(
        journey.admin,
        f"{BASE}/{deployment['id']}",
        {"description": "Changed"},
        method="PATCH",
        etag='"v1"',
    )
    assert (
        _write(journey.admin, path, {"materialIndex": 0}, etag='"v2"', status=409).json()["code"]
        == "MODEL_SOURCE_DOSSIER_INPUT_CHANGED"
    )
    stale = journey.admin.get(f"{path}/{blocked['id']}").json()
    assert stale["currentness"] == "STALE" and "CANDIDATE_CHANGED" in stale["currentnessReasons"]
    _write(journey.admin, f"{BASE}/{deployment['id']}:archive", {"reason": "Retired"}, etag='"v2"')
    _write(journey.admin, path, {"materialIndex": 0}, etag='"v3"', status=409)
    assert (
        _write(
            journey.admin,
            path,
            {"materialIndex": 0},
            etag='"v1"',
            key="source-unconfigured",
            status=201,
        ).json()
        == blocked
    )
    assert journey.admin.get(foreign + f"/{blocked['id']}").status_code == 404
    assert journey.admin.get(path, params={"cursor": "bad-cursor"}).status_code == 422


def test_source_check_audit_failure_rolls_back_record_and_receipt(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    *_, path = configured(journey, tmp_path, monkeypatch)

    class FailingAudit:
        def append_in_transaction(self, db: Any, envelope: Any) -> None:
            raise RuntimeError("synthetic source audit failure")

    runtime = bootstrap.model_gateway_http_runtime()
    broken = replace(runtime, dependencies=replace(runtime.dependencies, audit=FailingAudit()))
    with patch.object(bootstrap, "model_gateway_http_runtime", return_value=broken):
        with TestClient(
            bootstrap.create_app(), base_url="https://testserver", raise_server_exceptions=False
        ) as client:
            client.cookies.update(journey.admin.cookies)
            failed = client.post(
                path,
                json={"materialIndex": 0},
                headers={
                    **SAME_ORIGIN,
                    "If-Match": '"v1"',
                    "Idempotency-Key": "source-audit-rollback",
                },
            )
            assert failed.status_code == 500
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM model_gateway.material_source_check")
            ).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM model_gateway.idempotency_record "
                    "WHERE idempotency_key='source-audit-rollback'"
                )
            ).scalar_one()
            == 0
        )
    _write(
        journey.admin,
        path,
        {"materialIndex": 0},
        etag='"v1"',
        key="source-audit-rollback",
        status=201,
    )


@pytest.mark.parametrize("archive", [False, True])
def test_source_check_serializes_candidate_changes(
    journey: Journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: bool
) -> None:
    deployment, _, _, _, _, path = configured(journey, tmp_path, monkeypatch)
    started, release = Event(), Event()
    inspect = FileModelMaterialSource.inspect

    def paused(
        port: FileModelMaterialSource, reference: str, version: str | None
    ) -> SourceInspection:
        started.set()
        assert release.wait(10)
        return inspect(port, reference, version)

    monkeypatch.setattr(FileModelMaterialSource, "inspect", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        create = pool.submit(
            _write, journey.admin, path, {"materialIndex": 0}, etag='"v1"', status=201
        )
        try:
            assert started.wait(10)
            update = pool.submit(
                _write,
                journey.admin,
                f"{BASE}/{deployment['id']}" + (":archive" if archive else ""),
                {"reason": "Retired"} if archive else {"displayName": "Changed"},
                method="POST" if archive else "PATCH",
                etag='"v1"',
            )
            with pytest.raises(TimeoutError):
                update.result(timeout=0.2)
        finally:
            release.set()
        receipt = create.result(timeout=10).json()
        assert update.result(timeout=10).json()["revision"] == 2
    detail = journey.admin.get(f"{path}/{receipt['id']}").json()
    assert detail["snapshot"]["candidateRevision"] == 1
    assert detail["snapshot"]["result"] == "MATCHED" and detail["currentness"] == "STALE"
