from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from threading import Event, Lock
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse
from sqlalchemy import Engine, create_engine, text

from control_plane.app.modules.authorization import (
    V02_SUPER_ADMIN_PLATFORM_CAPABILITIES,
    AuthorizationPrincipal,
    Scope,
    grant,
)
from control_plane.app.modules.authorization.adapters import (
    SqlAlchemyAuthorizationRepository,
    SqlAlchemyIdentitySessionValidator,
)
from control_plane.app.modules.authorization.api.routes import (
    AUTHORIZATION_MANAGE_CAPABILITY,
    AuthorizationHttpRuntime,
    create_authorization_router,
)
from control_plane.app.modules.authorization.application import (
    AuthorizationDependencies,
    DecisionDependencies,
)
from control_plane.app.modules.authorization.ports import WorkspaceMembershipPort
from control_plane.app.modules.identity import SessionKind, SessionPrincipal
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from tests.authorization.helpers import authorization_dependencies
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.identity.test_auth_flow import _initialize_account

pytestmark = pytest.mark.integration


def _runtime(
    authorization_engine: Engine,
    identity_engine: Engine,
    membership: WorkspaceMembershipPort,
    dependencies: AuthorizationDependencies | None = None,
) -> AuthorizationHttpRuntime:
    return AuthorizationHttpRuntime(
        engine=authorization_engine,
        dependencies=dependencies or authorization_dependencies(),
        decision_dependencies=DecisionDependencies(
            identity=SqlAlchemyIdentitySessionValidator(
                identity_engine,
                identity_dependencies(),
            ),
            workspace=membership,
        ),
        organization_summary=lambda account_id: {
            "accountId": account_id,
            "kind": "LEADER",
            "superiorId": "manager-1",
        },
        workspace_summaries=lambda account_id: [
            {"id": "workspace-1", "name": "Workspace One", "ownerId": account_id}
        ],
    )


class AlwaysMember:
    def is_formal_member(self, workspace_id: str, account_id: str) -> bool:
        return True


def _client(runtime_provider: Callable[[], AuthorizationHttpRuntime]) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(create_authorization_router(runtime_provider))
    return TestClient(
        app,
        base_url="https://testserver",
        raise_server_exceptions=False,
    )


def _session_actor(account_id: str) -> SessionPrincipal:
    return SessionPrincipal(
        account_id=account_id,
        employee_no="00000001",
        display_name="Alice",
        session_kind=SessionKind.FULL,
        is_super_admin=False,
    )


def _management_client(
    *,
    authorization_engine: Engine,
    identity_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    dependencies: AuthorizationDependencies | None = None,
) -> tuple[TestClient, str]:
    _secret, token = _initialize_account(
        identity_engine,
        identity_dependencies(),
        monkeypatch,
    )
    with owner_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())
    with authorization_engine.begin() as db:
        grant(
            db,
            principal_id=account_id,
            capability=AUTHORIZATION_MANAGE_CAPABILITY,
            scope=Scope.platform(),
            actor=_session_actor(account_id),
            reason="manage grants",
            dependencies=authorization_dependencies(),
        )
    runtime = _runtime(
        authorization_engine,
        identity_engine,
        AlwaysMember(),
        dependencies,
    )
    client = _client(lambda: runtime)
    client.cookies.set("ep_session", token)
    return client, account_id


def test_super_admin_me_and_navigation_are_exact_v02_projection(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secret, token = _initialize_account(
        authorization_identity_engine,
        identity_dependencies(),
        monkeypatch,
    )
    with authorization_owner_engine.begin() as db:
        account_id = str(
            db.execute(
                text(
                    "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                    "RETURNING id"
                )
            ).scalar_one()
        )
    with authorization_rw_engine.begin() as db:
        db.execute(
            text(
                'INSERT INTO "authorization".principal_version '
                "(account_id, version, fence_generation, updated_at) "
                "VALUES (:account_id, 1, 0, now())"
            ),
            {"account_id": account_id},
        )

    runtime = _runtime(authorization_rw_engine, authorization_identity_engine, AlwaysMember())
    client = _client(lambda: runtime)
    client.cookies.set("ep_session", token)

    me = client.get("/api/v1/me")
    navigation = client.get("/api/v1/navigation")

    assert me.status_code == 200
    assert navigation.status_code == 200
    expected_route_keys = [
        "home",
        "admin",
        "audit",
        "admin.workspaces",
        "admin.organization",
        "admin.users",
        "admin.grants",
        "admin.policies",
    ]
    assert [item["routeKey"] for item in navigation.json()] == expected_route_keys
    assert {item["capability"] for item in me.json()["capabilities"]} == set(
        V02_SUPER_ADMIN_PLATFORM_CAPABILITIES
    )
    assert not {
        "tasks",
        "workspaces",
        "admin.skills",
        "admin.models",
        "admin.roles",
        "admin.menus",
    } & set(expected_route_keys)


def test_real_me_navigation_and_grant_lifecycle_are_protected_and_compatible(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secret, token = _initialize_account(
        authorization_identity_engine,
        identity_dependencies(),
        monkeypatch,
    )
    with authorization_owner_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())
        before = db.execute(
            text("SELECT last_seen_at FROM identity.session WHERE kind='FULL'")
        ).scalar_one()
    deps = authorization_dependencies()
    actor = _session_actor(account_id)
    with authorization_rw_engine.begin() as db:
        grant(
            db,
            principal_id=account_id,
            capability="platform.home.read",
            scope=Scope.platform(),
            actor=actor,
            reason="home navigation",
            dependencies=deps,
        )
        grant(
            db,
            principal_id=account_id,
            capability=AUTHORIZATION_MANAGE_CAPABILITY,
            scope=Scope.platform(),
            actor=actor,
            reason="manage grants",
            dependencies=deps,
        )
        grant(
            db,
            principal_id=account_id,
            capability="requirement.read",
            scope=Scope.workspace("workspace-1"),
            actor=actor,
            reason="workspace requirement navigation",
            dependencies=deps,
        )
    runtime = _runtime(authorization_rw_engine, authorization_identity_engine, AlwaysMember())
    client = _client(lambda: runtime)
    client.cookies.set("ep_session", token)

    me = client.get("/api/v1/me", headers={"X-Request-ID": "me-request"})
    assert me.status_code == 200
    assert me.json()["employeeId"] == "00000001"
    assert me.json()["name"] == "Alice"
    assert me.json()["accountId"] == account_id
    assert me.json()["organization"]["kind"] == "LEADER"
    assert me.json()["workspaces"][0]["id"] == "workspace-1"
    assert {
        (item["capability"], item["scopeType"], item.get("scopeId"))
        for item in me.json()["capabilities"]
    } >= {
        ("platform.home.read", "PLATFORM", None),
        (AUTHORIZATION_MANAGE_CAPABILITY, "PLATFORM", None),
    }

    navigation = client.get("/api/v1/navigation")
    assert navigation.status_code == 200
    assert navigation.json() == [
        {
            "routeKey": "home",
            "name": "首页",
            "order": 1,
            "capability": "platform.home.read",
            "scopeType": "PLATFORM",
            "meta": {"name": "首页", "order": 1},
        },
        {
            "routeKey": "admin.grants",
            "name": "Grant 管理",
            "order": 14,
            "capability": AUTHORIZATION_MANAGE_CAPABILITY,
            "scopeType": "PLATFORM",
            "meta": {"name": "Grant 管理", "order": 14},
        },
        {
            "routeKey": "requirements",
            "name": "Requirements",
            "order": 20,
            "capability": "requirement.read",
            "scopeType": "WORKSPACE",
            "meta": {
                "name": "Requirements",
                "order": 20,
                "actionCapabilities": [
                    {"capability": "work_item.create", "scopeType": "WORKSPACE"},
                    {"capability": "work_item.assign", "scopeType": "WORKSPACE"},
                    {
                        "capability": "requirement.baseline.submit",
                        "scopeType": "WORKSPACE",
                    },
                    {
                        "capability": "requirement.baseline.assign",
                        "scopeType": "WORKSPACE",
                    },
                    {
                        "capability": "requirement.baseline.decide",
                        "scopeType": "WORKSPACE",
                    },
                ],
            },
        },
    ]

    create_headers = {
        "Origin": "https://testserver",
        "Idempotency-Key": "grant-create-001",
    }
    create_body = {
        "principalId": "target-account",
        "capability": "platform.organization.read",
        "scopeType": "PLATFORM",
        "source": "MANUAL",
        "reason": "organization viewer",
    }
    created = client.post(
        "/api/v1/admin/grants",
        headers=create_headers,
        json=create_body,
    )
    assert created.status_code == 201
    assert created.headers["etag"] == '"v1"'
    grant_id = created.json()["id"]
    create_replay = client.post(
        "/api/v1/admin/grants",
        headers=create_headers,
        json=create_body,
    )
    assert create_replay.status_code == 201
    assert create_replay.json() == created.json()
    assert create_replay.headers["etag"] == '"v1"'
    create_conflict = client.post(
        "/api/v1/admin/grants",
        headers=create_headers,
        json={**create_body, "reason": "different command"},
    )
    assert create_conflict.status_code == 409

    listed = client.get("/api/v1/admin/grants")
    assert listed.status_code == 200
    assert grant_id in {item["id"] for item in listed.json()["items"]}

    revoked = client.request(
        "DELETE",
        f"/api/v1/admin/grants/{grant_id}",
        headers={
            "Origin": "https://testserver",
            "Idempotency-Key": "grant-revoke-001",
            "If-Match": '"v1"',
        },
        json={"reason": "access removed"},
    )
    assert revoked.status_code == 200
    assert revoked.headers["etag"] == '"v2"'
    assert revoked.json()["status"] == "REVOKED"
    revoke_replay = client.request(
        "DELETE",
        f"/api/v1/admin/grants/{grant_id}",
        headers={
            "Origin": "https://testserver",
            "Idempotency-Key": "grant-revoke-001",
            "If-Match": '"v1"',
        },
        json={"reason": "access removed"},
    )
    assert revoke_replay.status_code == 200
    assert revoke_replay.json() == revoked.json()
    assert revoke_replay.headers["etag"] == '"v2"'

    manage_grant = next(
        item
        for item in listed.json()["items"]
        if item["principalId"] == account_id
        and item["capability"] == AUTHORIZATION_MANAGE_CAPABILITY
    )
    self_revoke = client.request(
        "DELETE",
        f"/api/v1/admin/grants/{manage_grant['id']}",
        headers={
            "Origin": "https://testserver",
            "Idempotency-Key": "grant-self-revoke-001",
            "If-Match": '"v1"',
        },
        json={"reason": "remove own grant administration"},
    )
    assert self_revoke.status_code == 200
    denied_next = client.get(
        "/api/v1/admin/grants",
        headers={"X-Request-ID": "req-deniednext"},
    )
    assert denied_next.status_code == 403
    assert denied_next.json()["requestId"] == "req-deniednext"

    with authorization_owner_engine.connect() as db:
        after = db.execute(
            text("SELECT last_seen_at FROM identity.session WHERE kind='FULL'")
        ).scalar_one()
        denial = db.execute(
            text(
                "SELECT result FROM audit.audit_event "
                "WHERE action='authorization.decision' "
                "AND correlation_id='req-deniednext'"
            )
        ).scalar_one()
    assert after == before
    assert denial == "DENY"


def test_concurrent_same_key_creates_one_grant_and_exact_response(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    insert_lock = Lock()
    inserts = 0

    class BlockingRepository(SqlAlchemyAuthorizationRepository):
        def insert_grant(self, **values: Any) -> Any:
            nonlocal inserts
            if values["principal_id"] == "target-concurrent":
                with insert_lock:
                    inserts += 1
                entered.set()
                assert release.wait(timeout=5)
            return super().insert_grant(**values)

    dependencies = replace(
        authorization_dependencies(),
        repository_factory=BlockingRepository,
    )
    client, _account_id = _management_client(
        authorization_engine=authorization_rw_engine,
        identity_engine=authorization_identity_engine,
        owner_engine=authorization_owner_engine,
        monkeypatch=monkeypatch,
        dependencies=dependencies,
    )
    body = {
        "principalId": "target-concurrent",
        "capability": "platform.organization.read",
        "scopeType": "PLATFORM",
        "source": "MANUAL",
        "reason": "concurrent create",
    }
    headers = {
        "Origin": "https://testserver",
        "Idempotency-Key": "grant-concurrent-create-001",
    }

    def send() -> HttpxResponse:
        return cast(
            HttpxResponse,
            client.post("/api/v1/admin/grants", headers=headers, json=body),
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
        responses = [first.result(timeout=5), second.result(timeout=5)]

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert responses[0].headers["etag"] == responses[1].headers["etag"] == '"v1"'
    assert inserts == 1
    with authorization_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT "
                '(SELECT count(*) FROM "authorization"."grant" '
                " WHERE principal_id='target-concurrent'), "
                '(SELECT count(*) FROM "authorization".idempotency_record '
                " WHERE idempotency_key='grant-concurrent-create-001'), "
                "(SELECT count(*) FROM audit.audit_event e "
                ' JOIN "authorization"."grant" g ON g.id::text=e.target_id '
                " WHERE e.action='authorization.grant.created' "
                " AND g.principal_id='target-concurrent')"
            )
        ).one()
    assert counts == (1, 1, 1)


def test_tampered_grant_replay_fails_closed_without_reexecution(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _account_id = _management_client(
        authorization_engine=authorization_rw_engine,
        identity_engine=authorization_identity_engine,
        owner_engine=authorization_owner_engine,
        monkeypatch=monkeypatch,
    )
    body = {
        "principalId": "target-tamper",
        "capability": "platform.organization.read",
        "scopeType": "PLATFORM",
        "reason": "tamper proof",
    }
    headers = {
        "Origin": "https://testserver",
        "Idempotency-Key": "grant-tamper-create-001",
    }
    created = client.post("/api/v1/admin/grants", headers=headers, json=body)
    with authorization_owner_engine.begin() as db:
        db.execute(
            text(
                'UPDATE "authorization".idempotency_record '
                "SET sealed_response=:tampered "
                "WHERE idempotency_key='grant-tamper-create-001'"
            ),
            {"tampered": b"tampered"},
        )
    replay = client.post("/api/v1/admin/grants", headers=headers, json=body)

    assert created.status_code == 201
    assert replay.status_code == 409
    assert replay.json()["title"] == "Idempotency replay unavailable"
    with authorization_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT "
                '(SELECT count(*) FROM "authorization"."grant" '
                " WHERE principal_id='target-tamper'), "
                '(SELECT version FROM "authorization".principal_version '
                " WHERE account_id='target-tamper'), "
                "(SELECT count(*) FROM audit.audit_event e "
                ' JOIN "authorization"."grant" g ON g.id::text=e.target_id '
                " WHERE e.action='authorization.grant.created' "
                " AND g.principal_id='target-tamper')"
            )
        ).one()
    assert counts == (1, 2, 1)


def test_unexpected_grant_failure_rolls_back_fact_audit_version_and_claim(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCompletionRepository(SqlAlchemyAuthorizationRepository):
        def complete_idempotency(self, record_id: str, **values: Any) -> bool:
            completed = super().complete_idempotency(record_id, **values)
            assert completed
            raise RuntimeError("injected completion failure")

    dependencies = replace(
        authorization_dependencies(),
        repository_factory=FailingCompletionRepository,
    )
    client, _account_id = _management_client(
        authorization_engine=authorization_rw_engine,
        identity_engine=authorization_identity_engine,
        owner_engine=authorization_owner_engine,
        monkeypatch=monkeypatch,
        dependencies=dependencies,
    )
    response = client.post(
        "/api/v1/admin/grants",
        headers={
            "Origin": "https://testserver",
            "Idempotency-Key": "grant-rollback-create-001",
        },
        json={
            "principalId": "target-rollback",
            "capability": "platform.organization.read",
            "scopeType": "PLATFORM",
            "reason": "must roll back",
        },
    )

    assert response.status_code == 500
    assert "completion failure" not in response.text
    with authorization_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT "
                '(SELECT count(*) FROM "authorization"."grant" '
                " WHERE principal_id='target-rollback'), "
                '(SELECT count(*) FROM "authorization".principal_version '
                " WHERE account_id='target-rollback'), "
                '(SELECT count(*) FROM "authorization".idempotency_record '
                " WHERE idempotency_key='grant-rollback-create-001'), "
                "(SELECT count(*) FROM audit.audit_event "
                " WHERE action='authorization.grant.created' "
                " AND reason LIKE '%principal=target-rollback;%')"
            )
        ).one()
    assert counts == (0, 0, 0, 0)


def test_concurrent_revoke_has_one_winner_and_one_durable_stale_denial(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    target_grant_id = ""

    class BlockingRevokeRepository(SqlAlchemyAuthorizationRepository):
        def grant_by_id(self, grant_id: str, *, for_update: bool = False) -> Any:
            row = super().grant_by_id(grant_id, for_update=for_update)
            if grant_id == target_grant_id and for_update and row["status"] == "ACTIVE":
                entered.set()
                assert release.wait(timeout=5)
            return row

    dependencies = replace(
        authorization_dependencies(),
        repository_factory=BlockingRevokeRepository,
    )
    client, account_id = _management_client(
        authorization_engine=authorization_rw_engine,
        identity_engine=authorization_identity_engine,
        owner_engine=authorization_owner_engine,
        monkeypatch=monkeypatch,
        dependencies=dependencies,
    )
    with authorization_rw_engine.begin() as db:
        created = grant(
            db,
            principal_id="target-revoke",
            capability="platform.organization.read",
            scope=Scope.platform(),
            actor=_session_actor(account_id),
            reason="concurrent revoke target",
            dependencies=authorization_dependencies(),
        )
    target_grant_id = created.id

    def send(key: str) -> HttpxResponse:
        return cast(
            HttpxResponse,
            client.request(
                "DELETE",
                f"/api/v1/admin/grants/{target_grant_id}",
                headers={
                    "Origin": "https://testserver",
                    "Idempotency-Key": key,
                    "If-Match": '"v1"',
                },
                json={"reason": "concurrent revoke"},
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, "grant-race-revoke-001")
        assert entered.wait(timeout=3)
        second = pool.submit(send, "grant-race-revoke-002")
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.5)
        finally:
            release.set()
        responses = [first.result(timeout=5), second.result(timeout=5)]

    assert [response.status_code for response in responses] == [200, 409]
    assert responses[0].headers["etag"] == '"v2"'
    assert responses[1].json()["title"] == "Stale grant version"
    with authorization_owner_engine.connect() as db:
        status, version, principal_version, audits, commands = db.execute(
            text(
                "SELECT g.status, g.version, pv.version, "
                "(SELECT count(*) FROM audit.audit_event "
                " WHERE action='authorization.grant.revoked' AND target_id=g.id::text), "
                '(SELECT count(*) FROM "authorization".idempotency_record '
                " WHERE idempotency_key LIKE 'grant-race-revoke-%') "
                'FROM "authorization"."grant" g '
                'JOIN "authorization".principal_version pv '
                " ON pv.account_id=g.principal_id WHERE g.id=:id"
            ),
            {"id": target_grant_id},
        ).one()
    assert (status, version, principal_version, audits, commands) == (
        "REVOKED",
        2,
        3,
        1,
        2,
    )


def test_missing_cookie_and_unknown_authorization_version_fail_closed(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(authorization_rw_engine, authorization_identity_engine, AlwaysMember())
    client = _client(lambda: runtime)
    missing = client.get("/api/v1/me", headers={"X-Request-ID": "req-missingcookie"})
    assert missing.status_code == 401
    assert missing.json()["requestId"] == "req-missingcookie"

    _secret, token = _initialize_account(
        authorization_identity_engine,
        identity_dependencies(),
        monkeypatch,
    )
    client.cookies.set("ep_session", token)
    unknown = client.get("/api/v1/me", headers={"X-Request-ID": "req-unknownversion"})
    assert unknown.status_code == 503
    assert unknown.json() == {
        "title": "Authorization unavailable",
        "status": 503,
        "requestId": "req-unknownversion",
    }


def test_grant_write_preflight_rejects_before_session_validation(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    calls: list[str] = []

    class RecordingIdentity:
        def validate(self, raw_token: str) -> None:
            calls.append(raw_token)
            return None

    runtime = AuthorizationHttpRuntime(
        engine=authorization_rw_engine,
        dependencies=authorization_dependencies(),
        decision_dependencies=DecisionDependencies(
            identity=RecordingIdentity(),
            workspace=AlwaysMember(),
        ),
        organization_summary=lambda _account_id: None,
        workspace_summaries=lambda _account_id: [],
    )
    client = _client(lambda: runtime)
    response = client.post(
        "/api/v1/admin/grants",
        headers={"Origin": "https://evil.example"},
        json={
            "principalId": "target",
            "capability": "platform.organization.read",
            "scopeType": "PLATFORM",
            "reason": "should fail early",
        },
    )
    assert response.status_code in {403, 422}
    assert calls == []


@pytest.mark.parametrize(
    "failure_point,path",
    [
        ("principal_version", "/api/v1/me"),
        ("exact_grant", "/api/v1/admin/grants"),
        ("capability_projection", "/api/v1/me"),
    ],
)
def test_authorization_repository_read_failures_are_safe_503(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    path: str,
) -> None:
    request_id = f"req-{failure_point.replace('_', '')}"
    client, account_id = _management_client(
        authorization_engine=authorization_rw_engine,
        identity_engine=authorization_identity_engine,
        owner_engine=authorization_owner_engine,
        monkeypatch=monkeypatch,
    )
    token = client.cookies.get("ep_session")
    assert token is not None

    class FailingReadRepository(SqlAlchemyAuthorizationRepository):
        def principal_version(self, account_id: str, *, for_update: bool = False) -> Any:
            if failure_point == "principal_version":
                raise RuntimeError("SELECT principal_version FROM secret_dsn")
            return super().principal_version(account_id, for_update=for_update)

        def effective_grants(self, **values: Any) -> list[Any]:
            if failure_point == "exact_grant" and values["capability"] is not None:
                raise RuntimeError("SELECT exact grant FROM secret_dsn")
            if failure_point == "capability_projection" and values["capability"] is None:
                raise RuntimeError("SELECT projection FROM secret_dsn")
            return super().effective_grants(**values)

    runtime = _runtime(
        authorization_rw_engine,
        authorization_identity_engine,
        AlwaysMember(),
        replace(
            authorization_dependencies(),
            repository_factory=FailingReadRepository,
        ),
    )
    client = _client(lambda: runtime)
    client.cookies.set("ep_session", token)
    response = client.get(path, headers={"X-Request-ID": request_id})

    assert response.status_code == 503
    assert response.json() == {
        "title": "Authorization unavailable",
        "status": 503,
        "requestId": request_id,
    }
    assert "secret_dsn" not in response.text
    with authorization_owner_engine.connect() as db:
        audit_count = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='authorization.decision' "
                "AND correlation_id=:request_id AND result='UNAVAILABLE'"
            ),
            {"request_id": request_id},
        ).scalar_one()
    assert audit_count == 1
    assert account_id


def test_authorization_engine_connection_failure_is_safe_503_without_details() -> None:
    unavailable_engine = create_engine(
        "postgresql+psycopg://authorization_rw:redacted@127.0.0.1:59999/platform?connect_timeout=1",
        pool_pre_ping=True,
    )
    runtime = AuthorizationHttpRuntime(
        engine=unavailable_engine,
        dependencies=authorization_dependencies(),
        decision_dependencies=DecisionDependencies(
            identity=RecordingIdentityForUnavailableEngine(),
            workspace=AlwaysMember(),
        ),
        organization_summary=lambda _account_id: None,
        workspace_summaries=lambda _account_id: [],
    )
    try:
        client = _client(lambda: runtime)
        client.cookies.set("ep_session", "not-a-real-session-secret")
        response = client.get(
            "/api/v1/me",
            headers={"X-Request-ID": "req-engineunavailable"},
        )
    finally:
        unavailable_engine.dispose()

    assert response.status_code == 503
    assert response.json() == {
        "title": "Authorization unavailable",
        "status": 503,
        "requestId": "req-engineunavailable",
    }
    assert "59999" not in response.text
    assert "redacted" not in response.text


class RecordingIdentityForUnavailableEngine:
    def validate(self, raw_token: str) -> None:
        raise AssertionError(f"identity must not be reached: {raw_token}")


def test_resource_guard_engine_failure_is_safe_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import control_plane.app.bootstrap.app as bootstrap

    class UnavailableEngine:
        def begin(self) -> None:
            raise RuntimeError("postgresql://secret-resource-guard")

    runtime = AuthorizationHttpRuntime(
        engine=cast(Any, UnavailableEngine()),
        dependencies=authorization_dependencies(),
        decision_dependencies=DecisionDependencies(
            identity=RecordingIdentityForUnavailableEngine(),
            workspace=AlwaysMember(),
        ),
        organization_summary=lambda _account_id: None,
        workspace_summaries=lambda _account_id: [],
    )
    monkeypatch.setattr(bootstrap, "authorization_http_runtime", lambda: runtime)
    with pytest.raises(HTTPException) as captured:
        bootstrap.authorization_capability_guard(
            AuthorizationPrincipal(
                account_id="account-1",
                employee_id="00000001",
                name="Alice",
                is_super_admin=False,
                authorization_version=1,
                capabilities=(),
            ),
            "platform.workspace.manage",
            None,
        )
    assert captured.value.status_code == 503
    assert captured.value.detail == "Authorization unavailable"
