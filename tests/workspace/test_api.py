from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse
from sqlalchemy import Engine, text

import control_plane.app.modules.organization as organization
from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.identity import Principal, SessionKind, SessionPrincipal
from control_plane.app.modules.workspace import WorkspaceDependencies
from control_plane.app.modules.workspace.api.routes import (
    WorkspaceHttpRuntime,
    create_workspace_router,
)
from control_plane.app.modules.workspace.ports import DirectReportView
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from control_plane.app.shared.idempotency import SealedIdempotentEnvelope
from control_plane.app.shared.security import unseal
from tests.organization.helpers import insert_account, organization_dependencies
from tests.workspace.helpers import configure_org_leader, workspace_dependencies

pytestmark = pytest.mark.integration

MANAGER_ID = "00000000-0000-0000-0000-000000000901"
OWNER_ID = "00000000-0000-0000-0000-000000000902"
SECOND_LEADER_ID = "00000000-0000-0000-0000-000000000903"
SAME_ORIGIN = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}


def _session_principal(account_id: str = OWNER_ID) -> SessionPrincipal:
    return SessionPrincipal(
        account_id=account_id,
        employee_no="00000902" if account_id == OWNER_ID else "00000903",
        display_name="Owner" if account_id == OWNER_ID else "Second leader",
        session_kind=SessionKind.FULL,
        is_super_admin=False,
    )


def _isolated_client(
    workspace_rw_engine: Engine,
    dependencies: WorkspaceDependencies,
    *,
    principal: SessionPrincipal | None = None,
    guard: Callable[[SessionPrincipal, str, str | None], None] | None = None,
    runtime_provider: Callable[[], WorkspaceHttpRuntime] | None = None,
    principal_provider: Callable[[], SessionPrincipal] | None = None,
) -> TestClient:
    runtime = WorkspaceHttpRuntime(
        engine=workspace_rw_engine,
        dependencies=dependencies,
    )
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_workspace_router(
            runtime_provider=runtime_provider or (lambda: runtime),
            principal_provider=principal_provider or (lambda: principal or _session_principal()),
            capability_guard=guard or (lambda _principal, _capability, _scope_id: None),
        )
    )
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def _configure_two_leaders(
    owner_engine: Engine,
    identity_engine: Engine,
    organization_engine: Engine,
) -> None:
    rows = [
        (MANAGER_ID, "00000901", "Manager"),
        (OWNER_ID, "00000902", "Owner"),
        (SECOND_LEADER_ID, "00000903", "Second leader"),
    ]
    for account_id, employee_no, display_name in rows:
        insert_account(
            owner_engine,
            account_id=account_id,
            employee_no=employee_no,
            display_name=display_name,
        )
    deps = organization_dependencies(identity_engine, on_membership_change=lambda _ids: None)
    actor = Principal(employee_id="SYSTEM", name="System")
    with organization_engine.begin() as db:
        for account_id, superior_id in (
            (MANAGER_ID, None),
            (OWNER_ID, MANAGER_ID),
            (SECOND_LEADER_ID, MANAGER_ID),
        ):
            organization.set_superior(
                db,
                account_id=account_id,
                superior_id=superior_id,
                actor=actor,
                reason="workspace api fixture",
                dependencies=deps,
            )


def test_isolated_workspace_routes_expose_exact_contract_and_create_camel_response(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    configure_org_leader(
        owner_engine=workspace_owner_engine,
        organization_engine=workspace_organization_engine,
        identity_engine=workspace_identity_engine,
        manager_id=MANAGER_ID,
        leader_id=OWNER_ID,
    )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    guarded: list[tuple[str, str]] = []
    client = _isolated_client(
        workspace_rw_engine,
        dependencies,
        guard=lambda principal, capability, _scope_id: guarded.append(
            (principal.account_id, capability)
        ),
    )

    schema = client.get("/openapi.json").json()
    expected_operations: dict[str, tuple[str, str]] = {
        "/api/v1/admin/workspaces": ("get", "workspace_list"),
        "/api/v1/admin/workspaces#create": ("post", "workspace_create"),
        "/api/v1/admin/workspaces/{id}/leaders": ("post", "workspace_invite_leader"),
        "/api/v1/admin/workspaces/{id}/leaders/{accountId}": (
            "delete",
            "workspace_remove_leader",
        ),
        "/api/v1/admin/workspaces/{id}/transfer-owner": (
            "post",
            "workspace_transfer_owner",
        ),
        "/api/v1/admin/workspaces/{id}/members": ("get", "workspace_members"),
    }
    for key, (method, operation_id) in expected_operations.items():
        path = key.removesuffix("#create")
        assert schema["paths"][path][method]["operationId"] == operation_id

    response = client.post(
        "/api/v1/admin/workspaces",
        json={"name": "API Workspace", "ownerId": OWNER_ID, "reason": "create"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "workspace-create-0001"},
    )

    assert response.status_code == 201
    assert response.headers["etag"] == '"v1"'
    assert "set-cookie" not in response.headers
    assert response.json() == {
        "id": response.json()["id"],
        "name": "API Workspace",
        "ownerId": OWNER_ID,
        "archivedAt": None,
        "version": 1,
    }
    assert guarded == [(OWNER_ID, "platform.workspace.manage")]
    with workspace_owner_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM workspace.workspace")).scalar_one() == 1
        idempotency_actor = db.execute(
            text(
                "SELECT actor FROM workspace.idempotency_record WHERE operation='workspace_create'"
            )
        ).scalar_one()
        audit_actor = db.execute(
            text("SELECT actor FROM audit.audit_event WHERE action='workspace.created'")
        ).scalar_one()
    assert idempotency_actor == audit_actor == OWNER_ID


def test_workspace_routes_are_globally_registered_and_require_authentication() -> None:
    app = create_app()
    schema = app.openapi()
    operation_ids = {
        "workspace_list",
        "workspace_create",
        "workspace_invite_leader",
        "workspace_remove_leader",
        "workspace_transfer_owner",
        "workspace_members",
    }

    registered = {
        operation.get("operationId")
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict)
    }
    assert operation_ids <= registered
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    response = client.get("/api/v1/admin/workspaces")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


def test_write_headers_and_same_origin_are_enforced_before_database_access(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    configure_org_leader(
        owner_engine=workspace_owner_engine,
        organization_engine=workspace_organization_engine,
        identity_engine=workspace_identity_engine,
        manager_id=MANAGER_ID,
        leader_id=OWNER_ID,
    )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    calls: list[str] = []
    runtime = WorkspaceHttpRuntime(engine=workspace_rw_engine, dependencies=dependencies)

    def runtime_provider() -> WorkspaceHttpRuntime:
        calls.append("runtime")
        return runtime

    def principal_provider() -> SessionPrincipal:
        calls.append("principal")
        return _session_principal()

    client = _isolated_client(
        workspace_rw_engine,
        dependencies,
        runtime_provider=runtime_provider,
        principal_provider=principal_provider,
    )
    create_body = {"name": "Denied", "ownerId": OWNER_ID, "reason": "headers"}

    missing_key = client.post(
        "/api/v1/admin/workspaces",
        json=create_body,
        headers=SAME_ORIGIN,
    )
    cross_origin = client.post(
        "/api/v1/admin/workspaces",
        json=create_body,
        headers={"Origin": "https://evil.example", "Idempotency-Key": "workspace-key-0001"},
    )
    cross_site_metadata = client.post(
        "/api/v1/admin/workspaces",
        json=create_body,
        headers={
            "Sec-Fetch-Site": "cross-site",
            "Idempotency-Key": "workspace-key-0004",
        },
    )
    malformed_key = client.post(
        "/api/v1/admin/workspaces",
        json=create_body,
        headers={**SAME_ORIGIN, "Idempotency-Key": "short"},
    )
    missing_match = client.post(
        "/api/v1/admin/workspaces/missing/leaders",
        json={"accountId": OWNER_ID, "reason": "missing match"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "workspace-key-0002"},
    )
    invalid_match = client.post(
        "/api/v1/admin/workspaces/missing/leaders",
        json={"accountId": OWNER_ID, "reason": "invalid match"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "workspace-key-0003",
            "If-Match": 'W/"v1"',
        },
    )

    assert [
        response.status_code
        for response in (
            missing_key,
            cross_origin,
            cross_site_metadata,
            malformed_key,
            missing_match,
            invalid_match,
        )
    ] == [422, 403, 403, 422, 422, 422]
    assert all(
        response.headers["content-type"].startswith("application/problem+json")
        for response in (
            missing_key,
            cross_origin,
            cross_site_metadata,
            malformed_key,
            missing_match,
            invalid_match,
        )
    )
    assert all(
        response.json()["requestId"] == response.headers["x-request-id"]
        for response in (
            missing_key,
            cross_origin,
            cross_site_metadata,
            malformed_key,
            missing_match,
            invalid_match,
        )
    )
    assert calls == []
    with workspace_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM workspace.workspace), "
                "(SELECT count(*) FROM workspace.idempotency_record)"
            )
        ).one()
    assert counts == (0, 0)


def test_success_replay_conflict_and_tamper_are_durable_and_cookie_free(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    configure_org_leader(
        owner_engine=workspace_owner_engine,
        organization_engine=workspace_organization_engine,
        identity_engine=workspace_identity_engine,
        manager_id=MANAGER_ID,
        leader_id=OWNER_ID,
    )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    client = _isolated_client(workspace_rw_engine, dependencies)
    path = "/api/v1/admin/workspaces"
    headers = {**SAME_ORIGIN, "Idempotency-Key": "workspace-replay-0001"}
    body = {"name": "Replay", "ownerId": OWNER_ID, "reason": "create once"}

    first = client.post(path, json=body, headers=headers)
    replay = client.post(path, json=body, headers=headers)
    conflict = client.post(path, json={**body, "name": "Different"}, headers=headers)
    with workspace_owner_engine.begin() as db:
        db.execute(
            text(
                "UPDATE workspace.idempotency_record SET sealed_response=:tampered "
                "WHERE idempotency_key='workspace-replay-0001'"
            ),
            {"tampered": b"tampered"},
        )
    tampered = client.post(path, json=body, headers=headers)

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"] == '"v1"'
    assert "set-cookie" not in first.headers
    assert conflict.status_code == 409
    assert conflict.json()["title"] == "Idempotency conflict"
    assert tampered.status_code == 409
    assert tampered.json()["title"] == "Idempotency replay unavailable"
    with workspace_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM workspace.workspace), "
                "(SELECT count(*) FROM workspace.idempotency_record), "
                "(SELECT count(*) FROM audit.audit_event WHERE action LIKE 'workspace.%')"
            )
        ).one()
    assert counts == (1, 1, 2)


def test_deterministic_denial_replays_with_current_request_id_after_state_repair(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    insert_account(
        workspace_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000901",
        display_name="Manager",
    )
    insert_account(
        workspace_owner_engine,
        account_id=OWNER_ID,
        employee_no="00000902",
        display_name="Candidate",
    )
    org_deps = organization_dependencies(
        workspace_identity_engine,
        on_membership_change=lambda _ids: None,
    )
    with workspace_organization_engine.begin() as db:
        organization.set_superior(
            db,
            account_id=MANAGER_ID,
            superior_id=None,
            actor=Principal(employee_id="SYSTEM", name="System"),
            reason="manager one",
            dependencies=org_deps,
        )
        organization.set_superior(
            db,
            account_id=OWNER_ID,
            superior_id=None,
            actor=Principal(employee_id="SYSTEM", name="System"),
            reason="candidate is manager",
            dependencies=org_deps,
        )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    client = _isolated_client(workspace_rw_engine, dependencies)
    body = {"name": "Denied", "ownerId": OWNER_ID, "reason": "not leader"}
    key = "workspace-denial-0001"

    first = client.post(
        "/api/v1/admin/workspaces",
        json=body,
        headers={**SAME_ORIGIN, "Idempotency-Key": key, "X-Request-ID": "req-first"},
    )
    with workspace_organization_engine.begin() as db:
        organization.set_superior(
            db,
            account_id=OWNER_ID,
            superior_id=MANAGER_ID,
            actor=Principal(employee_id="SYSTEM", name="System"),
            reason="repair to leader",
            dependencies=org_deps,
        )
    replay = client.post(
        "/api/v1/admin/workspaces",
        json=body,
        headers={**SAME_ORIGIN, "Idempotency-Key": key, "X-Request-ID": "req-second"},
    )
    success = client.post(
        "/api/v1/admin/workspaces",
        json=body,
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "workspace-denial-repair-0001",
            "X-Request-ID": "req-third",
        },
    )

    assert first.json() == {
        "title": "Workspace governance conflict",
        "status": 409,
        "requestId": "req-first",
    }
    assert replay.json() == {
        "title": "Workspace governance conflict",
        "status": 409,
        "requestId": "req-second",
    }
    assert success.status_code == 201
    with workspace_owner_engine.connect() as db:
        denial = (
            db.execute(
                text(
                    "SELECT sealed_response FROM workspace.idempotency_record "
                    "WHERE idempotency_key=:key"
                ),
                {"key": key},
            )
            .mappings()
            .one()
        )
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM workspace.workspace), "
                "(SELECT count(*) FROM workspace.idempotency_record)"
            )
        ).one()
    envelope = SealedIdempotentEnvelope.model_validate_json(
        unseal(
            denial["sealed_response"],
            dependencies.secret_manager.load().idempotency_sealing_key,
        )
    )
    assert "requestId" not in envelope.response.body
    assert counts == (1, 2)


def test_unknown_members_is_a_not_found_problem_with_current_request_id(
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    client = _isolated_client(workspace_rw_engine, dependencies)

    response = client.get(
        "/api/v1/admin/workspaces/00000000-0000-0000-0000-000000000999/members",
        headers={"X-Request-ID": "req-membersmissing"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "title": "Workspace not found",
        "status": 404,
        "requestId": "req-membersmissing",
    }


def test_archived_members_fails_closed_without_projection_disclosure(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    configure_org_leader(
        owner_engine=workspace_owner_engine,
        organization_engine=workspace_organization_engine,
        identity_engine=workspace_identity_engine,
        manager_id=MANAGER_ID,
        leader_id=OWNER_ID,
    )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    client = _isolated_client(workspace_rw_engine, dependencies)
    created = client.post(
        "/api/v1/admin/workspaces",
        json={"name": "Archived", "ownerId": OWNER_ID, "reason": "create"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "workspace-archive-0001"},
    )
    workspace_id = created.json()["id"]
    with workspace_owner_engine.begin() as db:
        db.execute(
            text("UPDATE workspace.workspace SET archived_at=:archived_at WHERE id=:id"),
            {"archived_at": datetime.now(UTC), "id": workspace_id},
        )

    response = client.get(
        f"/api/v1/admin/workspaces/{workspace_id}/members",
        headers={"X-Request-ID": "req-membersarchived"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "title": "Workspace archived",
        "status": 409,
        "requestId": "req-membersarchived",
    }
    assert OWNER_ID not in response.text


def test_owner_denial_replays_precise_forbidden_problem_for_account_actor(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    _configure_two_leaders(
        workspace_owner_engine,
        workspace_identity_engine,
        workspace_organization_engine,
    )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    owner_client = _isolated_client(workspace_rw_engine, dependencies)
    created = owner_client.post(
        "/api/v1/admin/workspaces",
        json={"name": "Owner gate", "ownerId": OWNER_ID, "reason": "create"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "workspace-owner-gate-create"},
    )
    workspace_id = created.json()["id"]
    non_owner_client = _isolated_client(
        workspace_rw_engine,
        dependencies,
        principal=_session_principal(SECOND_LEADER_ID),
    )
    headers = {
        **SAME_ORIGIN,
        "Idempotency-Key": "workspace-owner-denial-0001",
        "If-Match": '"v1"',
    }
    path = f"/api/v1/admin/workspaces/{workspace_id}/leaders"
    body = {"accountId": SECOND_LEADER_ID, "reason": "non-owner invite"}

    first = non_owner_client.post(
        path,
        json=body,
        headers={**headers, "X-Request-ID": "req-ownerfirst"},
    )
    replay = non_owner_client.post(
        path,
        json=body,
        headers={**headers, "X-Request-ID": "req-ownerreplay"},
    )

    assert first.json() == {
        "title": "Workspace owner required",
        "status": 403,
        "requestId": "req-ownerfirst",
    }
    assert replay.json() == {
        "title": "Workspace owner required",
        "status": 403,
        "requestId": "req-ownerreplay",
    }
    with workspace_owner_engine.connect() as db:
        denial_actor, state = db.execute(
            text(
                "SELECT actor, state FROM workspace.idempotency_record "
                "WHERE idempotency_key='workspace-owner-denial-0001'"
            )
        ).one()
    assert denial_actor == SECOND_LEADER_ID
    assert state == "COMPLETED"


@pytest.mark.parametrize("failure_type", [RuntimeError, ValueError])
def test_unexpected_projection_failure_rolls_back_fact_audit_and_claim(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
    failure_type: type[Exception],
) -> None:
    configure_org_leader(
        owner_engine=workspace_owner_engine,
        organization_engine=workspace_organization_engine,
        identity_engine=workspace_identity_engine,
        manager_id=MANAGER_ID,
        leader_id=OWNER_ID,
    )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    real_organization = dependencies.organization

    class FailingOrganization:
        def is_effective_leader(self, account_id: str) -> bool:
            return real_organization.is_effective_leader(account_id)

        def direct_reports(self, leader_id: str) -> list[DirectReportView]:
            raise failure_type("projection query unavailable")

    failing = replace(dependencies, organization=FailingOrganization())
    client = _isolated_client(workspace_rw_engine, failing)
    response = client.post(
        "/api/v1/admin/workspaces",
        json={"name": "Rollback", "ownerId": OWNER_ID, "reason": "must rollback"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "workspace-rollback-0001"},
    )

    assert response.status_code == 500
    assert "projection" not in response.text.lower()
    with workspace_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM workspace.workspace), "
                "(SELECT count(*) FROM workspace.idempotency_record), "
                "(SELECT count(*) FROM audit.audit_event WHERE action LIKE 'workspace.%')"
            )
        ).one()
    assert counts == (0, 0, 0)


def test_concurrent_same_key_creates_one_fact_and_exact_response(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    configure_org_leader(
        owner_engine=workspace_owner_engine,
        organization_engine=workspace_organization_engine,
        identity_engine=workspace_identity_engine,
        manager_id=MANAGER_ID,
        leader_id=OWNER_ID,
    )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    real_organization = dependencies.organization
    entered = Event()
    release = Event()
    checks = 0

    class BlockingOrganization:
        def is_effective_leader(self, account_id: str) -> bool:
            return real_organization.is_effective_leader(account_id)

        def direct_reports(self, leader_id: str) -> list[DirectReportView]:
            nonlocal checks
            checks += 1
            entered.set()
            assert release.wait(timeout=5)
            return real_organization.direct_reports(leader_id)

    blocking = replace(dependencies, organization=BlockingOrganization())
    client = _isolated_client(workspace_rw_engine, blocking)
    body = {"name": "Concurrent", "ownerId": OWNER_ID, "reason": "same key"}
    headers = {**SAME_ORIGIN, "Idempotency-Key": "workspace-concurrent-0001"}

    def send() -> HttpxResponse:
        return cast(
            HttpxResponse, client.post("/api/v1/admin/workspaces", json=body, headers=headers)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send)
        assert entered.wait(timeout=3)
        second = pool.submit(send)
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.5)
        finally:
            release.set()
        responses = [first.result(timeout=3), second.result(timeout=3)]

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert checks == 1
    with workspace_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM workspace.workspace), "
                "(SELECT count(*) FROM workspace.idempotency_record), "
                "(SELECT count(*) FROM audit.audit_event WHERE action LIKE 'workspace.%')"
            )
        ).one()
    assert counts == (1, 1, 2)


def test_if_match_controls_leader_mutation_and_list_members_are_enveloped(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    _configure_two_leaders(
        workspace_owner_engine,
        workspace_identity_engine,
        workspace_organization_engine,
    )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    client = _isolated_client(workspace_rw_engine, dependencies)
    created = client.post(
        "/api/v1/admin/workspaces",
        json={"name": "Versioned", "ownerId": OWNER_ID, "reason": "create"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "workspace-versioned-0001"},
    )
    workspace_id = created.json()["id"]

    invited = client.post(
        f"/api/v1/admin/workspaces/{workspace_id}/leaders",
        json={"accountId": SECOND_LEADER_ID, "reason": "invite"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "workspace-invite-0001",
            "If-Match": '"v1"',
        },
    )
    stale = client.post(
        f"/api/v1/admin/workspaces/{workspace_id}/leaders",
        json={"accountId": SECOND_LEADER_ID, "reason": "stale"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "workspace-invite-stale-0001",
            "If-Match": '"v1"',
            "X-Request-ID": "req-stale",
        },
    )
    stale_replay = client.post(
        f"/api/v1/admin/workspaces/{workspace_id}/leaders",
        json={"accountId": SECOND_LEADER_ID, "reason": "stale"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "workspace-invite-stale-0001",
            "If-Match": '"v1"',
            "X-Request-ID": "req-stalereplay",
        },
    )
    listing = client.get("/api/v1/admin/workspaces")
    projected = client.get(f"/api/v1/admin/workspaces/{workspace_id}/members")

    assert invited.status_code == 200
    assert invited.headers["etag"] == '"v2"'
    assert stale.status_code == 409
    assert stale.json()["title"] == "Stale workspace version"
    assert stale.json()["requestId"] == "req-stale"
    assert stale_replay.status_code == 409
    assert stale_replay.json() == {
        "title": "Stale workspace version",
        "status": 409,
        "requestId": "req-stalereplay",
    }
    assert listing.json()["nextCursor"] is None
    assert [item["id"] for item in listing.json()["items"]] == [workspace_id]
    assert projected.json()["nextCursor"] is None
    assert {(item["accountId"], item["source"]) for item in projected.json()["items"]} == {
        (OWNER_ID, "OWNER"),
        (SECOND_LEADER_ID, "LEADER"),
    }
