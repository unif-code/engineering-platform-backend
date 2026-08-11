from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Event
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse
from sqlalchemy import Engine, text

from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.identity import Principal
from control_plane.app.modules.organization import OrganizationDependencies
from control_plane.app.modules.organization.api.routes import (
    OrganizationHttpRuntime,
    create_organization_router,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from tests.organization.helpers import insert_account, organization_dependencies

pytestmark = pytest.mark.integration

MANAGER_ID = "00000000-0000-0000-0000-000000000401"
ACTOR = Principal(employee_id="00000999", name="Administrator")
SAME_ORIGIN = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}


def _isolated_client(
    organization_rw_engine: Engine,
    dependencies: OrganizationDependencies,
    *,
    guard: Callable[[Principal, str], None] | None = None,
) -> TestClient:
    runtime = OrganizationHttpRuntime(
        engine=organization_rw_engine,
        dependencies=dependencies,
    )
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_organization_router(
            runtime_provider=lambda: runtime,
            principal_provider=lambda: ACTOR,
            capability_guard=guard or (lambda _principal, _capability: None),
        )
    )
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def test_isolated_routes_have_fixed_operation_ids_and_camel_case_shape(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    insert_account(
        organization_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000401",
        display_name="Manager",
    )
    callbacks: list[tuple[str, ...]] = []
    guarded: list[tuple[str, str]] = []
    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=lambda ids: callbacks.append(tuple(ids)),
    )
    client = _isolated_client(
        organization_rw_engine,
        dependencies,
        guard=lambda principal, capability: guarded.append((principal.employee_id, capability)),
    )

    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/api/v1/admin/organization/tree"]["get"]["operationId"] == "org_tree"
    assert (
        schema["paths"]["/api/v1/admin/accounts/{accountId}/superior"]["put"]["operationId"]
        == "org_set_superior"
    )

    updated = client.put(
        f"/api/v1/admin/accounts/{MANAGER_ID}/superior",
        json={"superiorId": None, "reason": "create manager"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "org-manager-key-0001"},
    )
    tree = client.get("/api/v1/admin/organization/tree")

    assert updated.status_code == 204
    assert updated.content == b""
    assert tree.status_code == 200
    assert tree.json() == {
        "managers": [
            {
                "account": {
                    "id": MANAGER_ID,
                    "employeeNo": "00000401",
                    "displayName": "Manager",
                },
                "leaders": [],
            }
        ]
    }
    assert callbacks == [(MANAGER_ID,)]
    assert guarded == [
        (ACTOR.employee_id, "platform.organization.manage"),
        (ACTOR.employee_id, "platform.organization.read"),
    ]


def test_organization_routes_are_not_registered_in_real_bootstrap_or_artifact() -> None:
    app = create_app()
    schema = app.openapi()
    assert "/api/v1/admin/organization/tree" not in schema["paths"]
    assert "/api/v1/admin/accounts/{accountId}/superior" not in schema["paths"]
    assert not any(
        operation.get("operationId") in {"org_tree", "org_set_superior"}
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict)
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/v1/admin/organization/tree")
    assert response.status_code == 404
    with open("openapi.json", encoding="utf-8") as artifact:
        text_artifact = artifact.read()
    assert "org_tree" not in text_artifact
    assert "org_set_superior" not in text_artifact


def test_write_requires_same_origin_and_idempotency_key_before_database_change(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    insert_account(
        organization_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000401",
        display_name="Manager",
    )
    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=lambda _ids: None,
    )
    client = _isolated_client(organization_rw_engine, dependencies)
    path = f"/api/v1/admin/accounts/{MANAGER_ID}/superior"
    body = {"superiorId": None, "reason": "create manager"}

    missing_key = client.put(path, json=body, headers=SAME_ORIGIN)
    cross_origin = client.put(
        path,
        json=body,
        headers={"Origin": "https://evil.example", "Idempotency-Key": "org-key-00000001"},
    )

    assert missing_key.status_code == 422
    assert missing_key.headers["content-type"].startswith("application/problem+json")
    assert cross_origin.status_code == 403
    assert cross_origin.headers["content-type"].startswith("application/problem+json")
    with organization_owner_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM organization.org_edge")).scalar_one() == 0
        assert (
            db.execute(text("SELECT count(*) FROM organization.idempotency_record")).scalar_one()
            == 0
        )


def test_concurrent_duplicate_key_executes_fact_audit_and_callback_exactly_once(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    insert_account(
        organization_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000401",
        display_name="Manager",
    )
    callback_entered = Event()
    release_callback = Event()
    callbacks: list[tuple[str, ...]] = []

    def hold_callback(ids: object) -> None:
        callbacks.append(tuple(ids))  # type: ignore[arg-type]
        callback_entered.set()
        assert release_callback.wait(timeout=5)

    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=hold_callback,
    )
    client = _isolated_client(organization_rw_engine, dependencies)
    path = f"/api/v1/admin/accounts/{MANAGER_ID}/superior"
    body = {"superiorId": None, "reason": "concurrent create"}
    headers = {**SAME_ORIGIN, "Idempotency-Key": "org-concurrent-key-0001"}

    def send() -> HttpxResponse:
        return cast(HttpxResponse, client.put(path, json=body, headers=headers))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send)
        assert callback_entered.wait(timeout=3)
        second = pool.submit(send)
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.5)
        finally:
            release_callback.set()
        responses = [first.result(timeout=3), second.result(timeout=3)]

    assert [response.status_code for response in responses] == [204, 204]
    assert callbacks == [(MANAGER_ID,)]
    with organization_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM organization.org_edge) AS edges, "
                "(SELECT count(*) FROM organization.idempotency_record) AS commands, "
                "(SELECT count(*) FROM audit.audit_event "
                " WHERE action='organization.structure.changed') AS audits"
            )
        ).one()
    assert counts == (1, 1, 1)


def test_different_payload_conflicts_and_tampered_replay_fails_closed(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    insert_account(
        organization_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000401",
        display_name="Manager",
    )
    callbacks: list[tuple[str, ...]] = []
    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=lambda ids: callbacks.append(tuple(ids)),
    )
    client = _isolated_client(organization_rw_engine, dependencies)
    path = f"/api/v1/admin/accounts/{MANAGER_ID}/superior"
    headers = {**SAME_ORIGIN, "Idempotency-Key": "org-conflict-key-0001"}
    original_body = {"superiorId": None, "reason": "original"}

    first = client.put(path, json=original_body, headers=headers)
    conflict = client.put(
        path,
        json={"superiorId": None, "reason": "different"},
        headers=headers,
    )
    with organization_owner_engine.begin() as db:
        db.execute(
            text(
                "UPDATE organization.idempotency_record SET sealed_response=:tampered "
                "WHERE idempotency_key='org-conflict-key-0001'"
            ),
            {"tampered": b"tampered"},
        )
    tampered = client.put(path, json=original_body, headers=headers)

    assert first.status_code == 204
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")
    assert conflict.json()["title"] == "Idempotency conflict"
    assert tampered.status_code == 409
    assert tampered.headers["content-type"].startswith("application/problem+json")
    assert tampered.json()["title"] == "Idempotency replay unavailable"
    assert callbacks == [(MANAGER_ID,)]


def test_callback_failure_rolls_back_fact_audit_and_idempotency_claim(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    insert_account(
        organization_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000401",
        display_name="Manager",
    )

    def fail_callback(_ids: object) -> None:
        raise RuntimeError("projection unavailable")

    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=fail_callback,
    )
    client = _isolated_client(organization_rw_engine, dependencies)
    response = client.put(
        f"/api/v1/admin/accounts/{MANAGER_ID}/superior",
        json={"superiorId": None, "reason": "must roll back"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "org-rollback-key-0001"},
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    with organization_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM organization.org_edge) AS edges, "
                "(SELECT count(*) FROM organization.idempotency_record) AS commands, "
                "(SELECT count(*) FROM audit.audit_event "
                " WHERE action='organization.structure.changed') AS audits"
            )
        ).one()
    assert counts == (0, 0, 0)
