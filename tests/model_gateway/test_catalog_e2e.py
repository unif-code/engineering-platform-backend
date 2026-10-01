"""Real Sessions, default app, restricted PostgreSQL role; no model Provider credentials."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import control_plane.app.bootstrap.app as bootstrap
from tests.model_gateway.test_catalog_contract import PAYLOAD
from tests.source_control.test_v06_production_e2e import (
    Journey,
    _grant,
    _write,
)
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database
from tests.test_e2e_access_governance import SAME_ORIGIN

pytestmark = pytest.mark.integration
BASE = "/api/v1/admin/model-deployments"


def test_catalog_default_session_journey_replay_pagination_restart_and_audit(
    journey: Journey,
) -> None:
    admin = journey.admin
    empty = admin.get(BASE)
    assert empty.status_code == 200 and empty.json() == {"items": [], "nextCursor": None}
    route = next(
        row for row in admin.get("/api/v1/navigation").json() if row["routeKey"] == "admin.models"
    )
    assert route["meta"]["actionCapabilities"] == [
        {"capability": "platform.model.manage", "scopeType": "PLATFORM"}
    ]
    me = admin.get("/api/v1/me").json()
    assert {"platform.model.read", "platform.model.manage"} <= {
        item["capability"] for item in me["capabilities"] if item["scopeType"] == "PLATFORM"
    }
    created = _write(admin, BASE, PAYLOAD, status=201, key="catalog-create-one")
    assert created.headers["etag"] == '"v1"'
    candidate = created.json()
    assert candidate["state"] == "DRAFT" and candidate["revision"] == 1
    assert candidate["connectionRef"] is candidate["declaredContextWindow"] is None
    assert candidate["createdBy"] == candidate["updatedBy"] == me["accountId"]
    assert not {"verifiedCapabilities", "health", "price", "quota", "active"} & candidate.keys()
    path = f"{BASE}/{candidate['id']}"
    duplicate = _write(admin, BASE, PAYLOAD, status=201, key="catalog-create-one")
    assert duplicate.json() == candidate
    conflict = _write(
        admin,
        BASE,
        PAYLOAD | {"displayName": "Different intent"},
        status=409,
        key="catalog-create-one",
    )
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert (
        _write(admin, BASE, PAYLOAD, status=409).json()["code"] == "MODEL_DEPLOYMENT_KEY_CONFLICT"
    )
    detail = admin.get(path)
    assert detail.json() == candidate and detail.headers["etag"] == created.headers["etag"]

    update = {
        "displayName": "Changed candidate",
        "connectionRef": "model-connection:unconfigured",
        "declaredContextWindow": 100,
        "declaredMaxOutputTokens": 200,
        "description": "Declaration only",
        "declaredCapabilities": [],
    }
    edited = _write(admin, path, update, method="PATCH", etag='"v1"', key="catalog-edit-one")
    assert edited.json()["deploymentKey"] == PAYLOAD["deploymentKey"]
    assert edited.headers["etag"] == '"v2"' and edited.json()["declaredMaxOutputTokens"] == 200
    assert (
        _write(admin, path, update, method="PATCH", etag='"v1"', key="catalog-edit-one").json()
        == edited.json()
    )
    assert (
        _write(admin, path, update, method="PATCH", etag='"v1"', status=409).json()["code"]
        == "MODEL_DEPLOYMENT_REVISION_CONFLICT"
    )
    assert (
        _write(
            admin, path, update, method="PATCH", etag='"v2"', key="catalog-edit-one", status=409
        ).json()["code"]
        == "IDEMPOTENCY_CONFLICT"
    )
    cleared = _write(
        admin, path, {"connectionRef": None, "description": None}, method="PATCH", etag='"v2"'
    )
    assert cleared.json()["connectionRef"] is cleared.json()["description"] is None
    assert cleared.json()["declaredContextWindow"] == 100
    archived = _write(
        admin,
        path + ":archive",
        {"reason": "Candidate retired"},
        etag='"v3"',
        key="catalog-archive-one",
    )
    assert archived.json()["state"] == "ARCHIVED" and archived.headers["etag"] == '"v4"'
    assert archived.json()["archivedBy"] == me["accountId"]
    assert archived.json()["archiveReason"] == "Candidate retired"
    assert (
        _write(
            admin,
            path + ":archive",
            {"reason": "Candidate retired"},
            etag='"v3"',
            key="catalog-archive-one",
        ).json()
        == archived.json()
    )
    assert (
        _write(
            admin, path, {"displayName": "Reopen"}, method="PATCH", etag='"v4"', status=409
        ).json()["code"]
        == "MODEL_DEPLOYMENT_ARCHIVED"
    )
    _write(admin, path + ":archive", {"reason": "Again"}, etag='"v4"', status=409)
    _write(admin, BASE, PAYLOAD, status=409)
    _write(admin, BASE, PAYLOAD | {"deploymentKey": "candidate-two"}, status=201)
    first = admin.get(BASE, params={"pageSize": 1}).json()
    second = admin.get(BASE, params={"pageSize": 1, "cursor": first["nextCursor"]}).json()
    assert [row["deploymentKey"] for row in first["items"] + second["items"]] == [
        "candidate-one",
        "candidate-two",
    ]
    assert second["nextCursor"] is None
    assert (
        admin.get(BASE, params={"state": "DRAFT"}).json()["items"][0]["deploymentKey"]
        == "candidate-two"
    )
    assert admin.get(BASE, params={"state": "ARCHIVED", "q": "CHANGED"}).json()["items"] == [
        archived.json()
    ]
    assert admin.get(BASE, params={"q": "%"}).json()["items"] == []
    assert (
        admin.get(BASE, params={"q": "changed", "cursor": first["nextCursor"]}).status_code == 422
    )
    assert admin.get(BASE, params={"cursor": "not-base64"}).status_code == 422
    assert admin.get(f"{BASE}/{uuid4()}").status_code == 404
    assert admin.get(BASE, params={"pageSize": 0}).status_code == 422
    bootstrap.model_gateway_http_runtime.cache_clear()
    with TestClient(bootstrap.create_app(), base_url="https://testserver") as restarted:
        restarted.cookies.update(admin.cookies)
        assert restarted.get(path).json() == archived.json()
    with journey.database.owner.connect() as db:
        rows = (
            db.execute(
                text(
                    "SELECT action, reason FROM audit.audit_event "
                    "WHERE target_id=:id ORDER BY occurred_at"
                ),
                {"id": candidate["id"]},
            )
            .mappings()
            .all()
        )
        assert [row["action"] for row in rows] == [
            "model_deployment.created",
            "model_deployment.updated",
            "model_deployment.updated",
            "model_deployment.archived",
        ]
        assert all(
            "unconfigured" not in row["reason"] and "unverified-model-id" not in row["reason"]
            for row in rows
        )
        assert "Candidate retired" in rows[-1]["reason"]
    # The restricted login cannot delete, change creation/key facts, or bypass ARCHIVED.
    for sql in (
        "DELETE FROM model_gateway.deployment WHERE id=:id",
        "UPDATE model_gateway.deployment SET deployment_key='replaced' WHERE id=:id",
        "UPDATE model_gateway.deployment "
        "SET revision=revision+1, display_name='reopen' WHERE id=:id",
        "UPDATE audit.audit_event SET reason='tampered' WHERE target_id=:id",
    ):
        with pytest.raises(DBAPIError), journey.database.engines["model_gateway"].begin() as db:
            db.execute(text(sql), {"id": candidate["id"]})


def test_catalog_authority_scope_revocation_and_sensitive_input(journey: Journey) -> None:
    admin, member = journey.admin, journey.member
    assert member.get(BASE).status_code == 403
    _grant(admin, journey.member_id, "platform.model.read", journey.workspace_id)
    assert member.get(BASE).status_code == 403
    _grant(admin, journey.member_id, "platform.model.read")
    _grant(admin, journey.member_id, "platform.model.manage")
    assert member.get(BASE).status_code == 200
    assert "platform.model.manage" not in {
        item["capability"] for item in member.get("/api/v1/me").json()["capabilities"]
    }
    _write(member, BASE, PAYLOAD, status=403)
    assert any(row["routeKey"] == "admin.models" for row in member.get("/api/v1/navigation").json())
    created = _write(admin, BASE, PAYLOAD, status=201)
    path = f"{BASE}/{created.json()['id']}"
    _write(member, path, {"displayName": "Denied"}, method="PATCH", etag='"v1"', status=403)
    _write(member, path + ":archive", {"reason": "Denied"}, etag='"v1"', status=403)
    for bad in (
        {"apiKey": "sk-sensitive-value"},
        {"endpoint": "https://example.invalid"},
        {"connectionRef": "sk-sensitive-value"},
        {"connectionRef": "https://example.invalid"},
        {"description": "Bearer sk-sensitive-value"},
        {"state": "ACTIVE"},
        {"verifiedCapabilities": ["chat"]},
        {"declaredMaxOutputTokens": -1},
    ):
        rejected = _write(
            admin, BASE, PAYLOAD | {"deploymentKey": "invalid-candidate"} | bad, status=422
        )
        assert (
            "sk-sensitive-value" not in rejected.text
            and "https://example.invalid" not in rejected.text
        )
        assert rejected.headers["content-type"].startswith("application/problem+json")
        assert rejected.json()["requestId"]
    for bad in ({"deploymentKey": "different"}, {"state": "DRAFT"}, {"createdBy": "other"}, {}):
        _write(admin, path, bad, method="PATCH", etag='"v1"', status=422)
    assert (
        admin.post(
            BASE,
            json=PAYLOAD,
            headers={
                "Origin": "https://foreign.invalid",
                "Idempotency-Key": "catalog-cross-origin",
            },
        ).status_code
        == 403
    )
    assert admin.post(BASE, json=PAYLOAD, headers=SAME_ORIGIN).status_code == 422
    assert (
        admin.patch(
            path,
            json={"description": None},
            headers={**SAME_ORIGIN, "Idempotency-Key": "catalog-missing-etag"},
        ).status_code
        == 422
    )
    grants = admin.get("/api/v1/admin/grants").json()["items"]
    read_grant = next(
        row
        for row in grants
        if row["principalId"] == journey.member_id
        and row["capability"] == "platform.model.read"
        and row["scopeType"] == "PLATFORM"
    )
    _write(
        admin,
        f"/api/v1/admin/grants/{read_grant['id']}",
        {"reason": "Revoke directory read"},
        method="DELETE",
        etag=f'"v{read_grant["version"]}"',
    )
    assert member.get(BASE).status_code == 403
    _grant(admin, journey.member_id, "platform.model.read")
    account = next(
        row
        for row in admin.get("/api/v1/admin/accounts").json()["items"]
        if row["id"] == journey.member_id
    )
    _write(
        admin,
        f"/api/v1/admin/accounts/{journey.member_id}/disable",
        {"reason": "Disabled account"},
        etag=account["etag"],
        status=204,
    )
    assert member.get(BASE).status_code == 401
    with TestClient(bootstrap.create_app(), base_url="https://testserver") as anonymous:
        assert anonymous.get(BASE).status_code == 401
    with journey.database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM model_gateway.deployment")).scalar_one() == 1
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE target_type='MODEL_DEPLOYMENT'")
            ).scalar_one()
            == 1
        )


def test_catalog_concurrent_edit_archive_fences_and_atomic_audit(journey: Journey) -> None:
    created = _write(journey.admin, BASE, PAYLOAD, status=201)
    path = f"{BASE}/{created.json()['id']}"
    barrier = Barrier(2)

    def competing(archive: bool) -> int:
        with TestClient(bootstrap.create_app(), base_url="https://testserver") as client:
            client.cookies.update(journey.admin.cookies)
            barrier.wait(timeout=10)
            response = client.request(
                "POST" if archive else "PATCH",
                path + (":archive" if archive else ""),
                json={"reason": "Retire"} if archive else {"displayName": "Race winner"},
                headers={**SAME_ORIGIN, "If-Match": '"v1"', "Idempotency-Key": str(uuid4())},
            )
            return int(response.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(competing, (True, False))) == [200, 409]
    current = journey.admin.get(path)
    assert current.json()["revision"] == 2
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE target_id=:id"),
                {"id": created.json()["id"]},
            ).scalar_one()
            == 2
        )
    # Audit failure must roll back both the candidate and its durable idempotency claim.
    engine = journey.database.engines["model_gateway"]
    runtime = bootstrap.model_gateway_http_runtime()

    class FailingAudit:
        def append_in_transaction(self, db: object, envelope: object) -> None:
            raise RuntimeError("audit unavailable")

    from dataclasses import replace
    from unittest.mock import patch

    failing = replace(runtime, dependencies=replace(runtime.dependencies, audit=FailingAudit()))
    with patch.object(bootstrap, "model_gateway_http_runtime", return_value=failing):
        # The default router binds its provider at app creation.
        with TestClient(
            bootstrap.create_app(), base_url="https://testserver", raise_server_exceptions=False
        ) as client:
            client.cookies.update(journey.admin.cookies)
            failed = client.post(
                BASE,
                json=PAYLOAD | {"deploymentKey": "audit-rollback"},
                headers={**SAME_ORIGIN, "Idempotency-Key": "catalog-audit-rollback"},
            )
            assert failed.status_code == 500
    with engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM model_gateway.deployment "
                    "WHERE deployment_key='audit-rollback'"
                )
            ).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM model_gateway.idempotency_record "
                    "WHERE idempotency_key='catalog-audit-rollback'"
                )
            ).scalar_one()
            == 0
        )
    _write(
        journey.admin,
        BASE,
        PAYLOAD | {"deploymentKey": "audit-rollback"},
        key="catalog-audit-rollback",
        status=201,
    )
