from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.authorization import SecurityChangeOrchestrator
from control_plane.app.modules.identity import (
    AuthenticationDenial,
    BootstrapDenial,
    BootstrapPurpose,
    IdentityDependencies,
    IssuedSession,
    SessionKind,
    SessionPrincipal,
    enroll_totp,
    login_password_step,
    validate_session,
)
from control_plane.app.modules.identity.api.admin_routes import create_admin_account_router
from control_plane.app.modules.identity.api.auth_routes import IdentityHttpRuntime
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from control_plane.app.shared.db.settings import DbSettings
from control_plane.app.shared.idempotency import SealedIdempotentEnvelope
from control_plane.app.shared.security import unseal
from tests.authorization.conftest import temporary_authorization_role_engine
from tests.authorization.helpers import authorization_dependencies
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.identity.test_auth_flow import VALID_PASSWORD, _initialize_account
from tests.integration_database import parse_database_url

pytestmark = pytest.mark.integration

SAME_ORIGIN = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}


def _principal() -> SessionPrincipal:
    return SessionPrincipal(
        account_id="00000000-0000-0000-0000-000000000013",
        employee_no="00000999",
        display_name="Administrator",
        session_kind=SessionKind.FULL,
        is_super_admin=False,
    )


def _client(
    engine: Engine,
    dependencies: IdentityDependencies,
    *,
    guard: Callable[[object, str, str | None], None] | None = None,
    security_changes: object | None = None,
) -> TestClient:
    runtime = IdentityHttpRuntime(engine, dependencies, security_changes=security_changes)
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_admin_account_router(
            lambda: runtime,
            _principal,
            guard or (lambda _principal, _capability, _scope: None),
        )
    )
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


@pytest.fixture
def clean_admin_convergence(identity_owner_engine: Engine) -> Iterator[None]:
    tables = (
        '"authorization".convergence_principal_pending, '
        '"authorization".convergence_work, '
        '"authorization".principal_version'
    )
    with identity_owner_engine.begin() as db:
        db.execute(text(f"TRUNCATE {tables}"))
    yield None
    with identity_owner_engine.begin() as db:
        db.execute(text(f"TRUNCATE {tables}"))


@pytest.fixture(scope="session")
def admin_authorization_engine(identity_owner_engine: Engine) -> Iterator[Engine]:
    with temporary_authorization_role_engine(
        identity_owner_engine,
        parse_database_url(
            DbSettings().authorization_database_url,
            setting_name="AUTHORIZATION_DATABASE_URL",
        ),
    ) as runtime:
        yield runtime[0]


ADMIN_ACCOUNT_OPERATIONS = {
    ("/api/v1/admin/accounts", "get"): "accounts_list",
    ("/api/v1/admin/accounts", "post"): "create",
    ("/api/v1/admin/accounts/{id}/reset-password", "post"): "reset_password",
    ("/api/v1/admin/accounts/{id}/enable", "post"): "enable",
    ("/api/v1/admin/accounts/{id}/disable", "post"): "disable",
    ("/api/v1/admin/accounts/{id}/totp-reset", "post"): "totp_reset",
}


def test_openapi_declares_admin_account_operations_and_write_preflight() -> None:
    schema = create_app().openapi()

    for (path, method), operation_id in ADMIN_ACCOUNT_OPERATIONS.items():
        operation = schema["paths"][path][method]
        assert operation["operationId"] == operation_id
        assert set(operation["responses"]) >= {
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
            "503",
        }
        if method == "post":
            parameters = {value["name"]: value for value in operation["parameters"]}
            assert parameters["Idempotency-Key"]["required"] is True
            if path != "/api/v1/admin/accounts":
                assert parameters["If-Match"]["required"] is True
            success_status = {
                "create": "201",
                "reset_password": "200",
                "enable": "204",
                "disable": "204",
                "totp_reset": "204",
            }[operation_id]
            etag = operation["responses"][success_status]["headers"]["ETag"]
            assert etag["schema"]["type"] == "string"
            assert "version" in etag["description"].lower()
        rendered = str(operation).lower()
        assert "passwordhash" not in rendered
        assert "totpsealed" not in rendered
        assert "tokenhash" not in rendered
        assert "secrethash" not in rendered


def test_admin_account_preflight_rejects_before_runtime_access() -> None:
    client = TestClient(create_app(), base_url="https://testserver")

    missing_key = client.post(
        "/api/v1/admin/accounts",
        json={
            "employeeNo": "00000001",
            "displayName": "Alice",
            "reason": "onboarding",
        },
        headers={"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    cross_site = client.post(
        "/api/v1/admin/accounts",
        json={
            "employeeNo": "00000001",
            "displayName": "Alice",
            "reason": "onboarding",
        },
        headers={
            "Idempotency-Key": "admin-account-create-0001",
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert missing_key.status_code == 422
    assert cross_site.status_code == 403
    assert missing_key.headers["content-type"].startswith("application/problem+json")
    assert cross_site.headers["content-type"].startswith("application/problem+json")


def test_account_list_rejects_structurally_invalid_cursor_before_database() -> None:
    client = _client(
        engine=SimpleNamespace(connect=lambda: (_ for _ in ()).throw(AssertionError())),  # type: ignore[arg-type]
        dependencies=identity_dependencies(),
    )

    response = client.get(
        "/api/v1/admin/accounts",
        params={"cursor": "WyIwMDAwMDAwMSIsIm5vdC1hLXV1aWQiXQ"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(
    ("path", "needs_version"),
    [
        ("/api/v1/admin/accounts", False),
        ("/api/v1/admin/accounts/account-1/reset-password", True),
        ("/api/v1/admin/accounts/account-1/enable", True),
        ("/api/v1/admin/accounts/account-1/disable", True),
        ("/api/v1/admin/accounts/account-1/totp-reset", True),
    ],
)
def test_every_admin_write_rejects_before_session_or_database(
    path: str,
    needs_version: bool,
) -> None:
    client = TestClient(create_app(), base_url="https://testserver")
    body = (
        {"employeeNo": "00000001", "displayName": "Alice", "reason": "preflight"}
        if path == "/api/v1/admin/accounts"
        else {"reason": "preflight"}
    )
    missing_key = client.post(path, json=body, headers=SAME_ORIGIN)
    missing_version = client.post(
        path,
        json=body,
        headers={**SAME_ORIGIN, "Idempotency-Key": "account-preflight-0001"},
    )
    cross_site_headers = {
        "Idempotency-Key": "account-preflight-0002",
        "Origin": "https://attacker.example",
        "Sec-Fetch-Site": "cross-site",
    }
    if needs_version:
        cross_site_headers["If-Match"] = '"v1"'
    cross_site = client.post(path, json=body, headers=cross_site_headers)

    assert missing_key.status_code == 422
    assert missing_version.status_code == (422 if needs_version else 401)
    assert cross_site.status_code == 403


def test_all_routes_use_account_manage_at_platform_scope_without_runtime_access() -> None:
    guarded: list[tuple[str, str | None]] = []

    def deny(_principal: object, capability: str, scope: str | None) -> None:
        guarded.append((capability, scope))
        raise HTTPException(status_code=403, detail="Forbidden")

    client = _client(
        engine=SimpleNamespace(connect=lambda: (_ for _ in ()).throw(AssertionError())),  # type: ignore[arg-type]
        dependencies=identity_dependencies(),
        guard=deny,
    )
    requests = [
        client.get("/api/v1/admin/accounts"),
        client.post(
            "/api/v1/admin/accounts",
            json={"employeeNo": "00000001", "displayName": "Alice", "reason": "deny"},
            headers={**SAME_ORIGIN, "Idempotency-Key": "account-denied-0001"},
        ),
    ]
    for index, action in enumerate(("reset-password", "enable", "disable", "totp-reset"), 2):
        requests.append(
            client.post(
                f"/api/v1/admin/accounts/account-1/{action}",
                json={"reason": "deny"},
                headers={
                    **SAME_ORIGIN,
                    "Idempotency-Key": f"account-denied-000{index}",
                    "If-Match": '"v1"',
                },
            )
        )

    assert [response.status_code for response in requests] == [403] * 6
    assert guarded == [("identity.account.manage", None)] * 6


def test_create_replays_one_temporary_password_and_list_is_stably_redacted(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    client = _client(identity_rw_engine, identity_dependencies())
    headers = {**SAME_ORIGIN, "Idempotency-Key": "account-create-replay-0001"}
    body = {
        "employeeNo": "00000002",
        "displayName": "Bob",
        "profession": "Backend",
        "reason": "approved onboarding",
    }

    created = client.post("/api/v1/admin/accounts", json=body, headers=headers)
    replayed = client.post("/api/v1/admin/accounts", json=body, headers=headers)
    client.post(
        "/api/v1/admin/accounts",
        json={
            "employeeNo": "00000001",
            "displayName": "Alice",
            "reason": "approved onboarding",
        },
        headers={**SAME_ORIGIN, "Idempotency-Key": "account-create-order-0001"},
    )
    listing = client.get("/api/v1/admin/accounts?limit=1")
    next_page = client.get(
        "/api/v1/admin/accounts",
        params={"limit": 1, "cursor": listing.json()["nextCursor"]},
    )

    assert created.status_code == 201
    assert replayed.status_code == 201
    assert created.json() == replayed.json()
    assert created.headers["etag"] == replayed.headers["etag"] == '"v1"'
    assert len(created.json()["temporaryPassword"]) >= 24
    assert created.json()["account"] == {
        "id": created.json()["account"]["id"],
        "employeeNo": "00000002",
        "displayName": "Bob",
        "profession": "Backend",
        "status": "PENDING_INIT",
        "etag": '"v1"',
    }
    assert [
        listing.json()["items"][0]["employeeNo"],
        next_page.json()["items"][0]["employeeNo"],
    ] == [
        "00000001",
        "00000002",
    ]
    assert next_page.json()["nextCursor"] is None
    forbidden = {
        "passwordHash",
        "passwordSetAt",
        "temporaryPassword",
        "totpSealed",
        "totpConfirmedAt",
        "totpLastStep",
        "token",
        "tokenHash",
        "secretHash",
        "isSuperAdmin",
        "version",
    }
    assert forbidden.isdisjoint(listing.json()["items"][0])

    temporary_password = created.json()["temporaryPassword"]
    with identity_owner_engine.connect() as db:
        account_count, credential_hash = db.execute(
            text(
                "SELECT (SELECT count(*) FROM identity.account), "
                "(SELECT secret_hash FROM identity.temp_credential "
                "JOIN identity.account ON identity.account.id=account_id "
                "WHERE employee_no='00000002')"
            )
        ).one()
        audits = db.execute(
            text("SELECT actor, action, reason FROM audit.audit_event ORDER BY occurred_at, id")
        ).all()
        sealed_response = db.execute(
            text(
                "SELECT sealed_response FROM identity.idempotency_record "
                "WHERE idempotency_key='account-create-replay-0001'"
            )
        ).scalar_one()
    assert account_count == 2
    assert credential_hash != temporary_password
    assert all(temporary_password not in str(row) for row in audits)
    created_audit = next(row for row in audits if row.action == "identity.account.created")
    assert "beforeVersion=none; afterVersion=1" in created_audit.reason
    assert temporary_password.encode() not in sealed_response
    envelope = SealedIdempotentEnvelope.model_validate_json(
        unseal(
            sealed_response,
            identity_dependencies().secret_manager.load().idempotency_sealing_key,
        )
    )
    assert envelope.response.body["temporaryPassword"] == temporary_password


def test_duplicate_employee_number_is_one_safe_replayable_conflict(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    client = _client(identity_rw_engine, identity_dependencies())
    original = {
        "employeeNo": "00000001",
        "displayName": "Alice",
        "reason": "first onboarding",
    }
    duplicate = {**original, "displayName": "Impostor", "reason": "duplicate proof"}
    client.post(
        "/api/v1/admin/accounts",
        json=original,
        headers={**SAME_ORIGIN, "Idempotency-Key": "account-original-0001"},
    )
    first = client.post(
        "/api/v1/admin/accounts",
        json=duplicate,
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "account-duplicate-0001",
            "X-Request-ID": "req-duplicatefirst",
        },
    )
    replay = client.post(
        "/api/v1/admin/accounts",
        json=duplicate,
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "account-duplicate-0001",
            "X-Request-ID": "req-duplicatereplay",
        },
    )

    assert first.status_code == 409
    assert first.json() == {
        "title": "Account already exists",
        "status": 409,
        "requestId": "req-duplicatefirst",
    }
    assert replay.json() == {
        "title": "Account already exists",
        "status": 409,
        "requestId": "req-duplicatereplay",
    }
    with identity_owner_engine.connect() as db:
        denial_audits = db.execute(
            text(
                "SELECT actor, action, target_id, result, reason FROM audit.audit_event "
                "WHERE action='identity.account.create' AND result='DENIED'"
            )
        ).all()
        claims = db.execute(
            text(
                "SELECT count(*) FROM identity.idempotency_record "
                "WHERE idempotency_key='account-duplicate-0001'"
            )
        ).scalar_one()
    assert len(denial_audits) == 1
    assert denial_audits[0].actor == "00000999"
    assert "Impostor" not in str(denial_audits[0])
    assert claims == 1


def test_password_reset_revokes_old_access_preserves_totp_and_replays_secret(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _secret, old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.connect() as db:
        before = db.execute(
            text(
                "SELECT id, version, password_hash, totp_sealed, totp_confirmed_at "
                "FROM identity.account"
            )
        ).one()
    client = _client(identity_rw_engine, dependencies)
    headers = {
        **SAME_ORIGIN,
        "Idempotency-Key": "account-reset-replay-0001",
        "If-Match": f'"v{before.version}"',
    }
    path = f"/api/v1/admin/accounts/{before.id}/reset-password"

    reset = client.post(path, json={"reason": "verified reset"}, headers=headers)
    replay = client.post(path, json={"reason": "verified reset"}, headers=headers)

    assert reset.status_code == 200
    assert reset.json() == replay.json()
    assert len(reset.json()["temporaryPassword"]) >= 24
    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=old_session, dependencies=dependencies) is None
        old_password = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="reset-test",
            dependencies=dependencies,
        )
    assert isinstance(old_password, AuthenticationDenial)
    with identity_owner_engine.connect() as db:
        after = db.execute(
            text(
                "SELECT status, version, password_hash, password_set_at, "
                "totp_sealed, totp_confirmed_at FROM identity.account"
            )
        ).one()
        audit_rows = db.execute(
            text("SELECT action, reason FROM audit.audit_event ORDER BY occurred_at, id")
        ).all()
    assert after.status == "PENDING_INIT"
    assert after.version == before.version + 1
    assert after.password_hash is None
    assert after.password_set_at is None
    assert after.totp_sealed == before.totp_sealed
    assert after.totp_confirmed_at == before.totp_confirmed_at
    temporary_password = reset.json()["temporaryPassword"]
    assert all(temporary_password not in str(row) for row in audit_rows)
    password_audit = next(row for row in audit_rows if row.action == "identity.password.reset")
    assert (
        f"verified reset; beforeVersion={before.version}; afterVersion={after.version}"
        == password_audit.reason
    )


def test_disable_enable_use_current_version_and_revoke_sessions(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _secret, old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.connect() as db:
        account = db.execute(text("SELECT id, version FROM identity.account")).one()
    client = _client(identity_rw_engine, dependencies)
    disabled = client.post(
        f"/api/v1/admin/accounts/{account.id}/disable",
        json={"reason": "access removed"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "account-disable-0001",
            "If-Match": f'"v{account.version}"',
        },
    )
    stale = client.post(
        f"/api/v1/admin/accounts/{account.id}/enable",
        json={"reason": "stale restore"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "account-enable-stale-0001",
            "If-Match": f'"v{account.version}"',
            "X-Request-ID": "req-enablestale",
        },
    )
    enabled = client.post(
        f"/api/v1/admin/accounts/{account.id}/enable",
        json={"reason": "approved restore"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "account-enable-0001",
            "If-Match": f'"v{account.version + 1}"',
        },
    )

    assert disabled.status_code == 204
    assert disabled.content == b""
    assert disabled.headers["etag"] == f'"v{account.version + 1}"'
    assert stale.status_code == 409
    assert stale.json() == {
        "title": "Stale account version",
        "status": 409,
        "requestId": "req-enablestale",
    }
    assert enabled.status_code == 204
    assert enabled.content == b""
    assert enabled.headers["etag"] == f'"v{account.version + 2}"'
    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=old_session, dependencies=dependencies) is None
    with identity_owner_engine.connect() as db:
        status = db.execute(text("SELECT status FROM identity.account")).scalar_one()
        denials = db.execute(
            text("SELECT action, result, reason FROM audit.audit_event WHERE result='DENIED'")
        ).all()
        status_audits = (
            db.execute(
                text(
                    "SELECT reason FROM audit.audit_event "
                    "WHERE action='identity.account.status.changed' ORDER BY occurred_at, id"
                )
            )
            .scalars()
            .all()
        )
    assert status == "ENABLED"
    assert len(denials) == 1
    assert "stale" not in str(denials[0]).lower()
    assert status_audits == [
        f"access removed; beforeVersion={account.version}; afterVersion={account.version + 1}",
        (
            f"approved restore; beforeVersion={account.version + 1}; "
            f"afterVersion={account.version + 2}"
        ),
    ]


def test_concurrent_disable_keeps_one_effective_super_admin(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000001",
        display_name="Alice",
    )
    _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000002",
        display_name="Bob",
    )
    with identity_owner_engine.begin() as db:
        targets = db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                "RETURNING id, version"
            )
        ).all()
    client = _client(identity_rw_engine, dependencies)

    def disable(index: int) -> int:
        target = targets[index]
        response = client.post(
            f"/api/v1/admin/accounts/{target.id}/disable",
            json={"reason": f"concurrent disable {index}"},
            headers={
                **SAME_ORIGIN,
                "Idempotency-Key": f"account-disable-race-000{index}",
                "If-Match": f'"v{target.version}"',
            },
        )
        return int(response.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = [
            future.result(timeout=5)
            for future in (pool.submit(disable, 0), pool.submit(disable, 1))
        ]

    assert sorted(statuses) == [204, 409]
    with identity_owner_engine.connect() as db:
        effective = db.execute(
            text(
                "SELECT count(*) FROM identity.account WHERE is_super_admin=true "
                "AND status='ENABLED' AND password_hash IS NOT NULL "
                "AND totp_confirmed_at IS NOT NULL"
            )
        ).scalar_one()
    assert effective == 1


def test_totp_reset_preserves_password_and_requires_new_enrollment(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    old_secret, old_session = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
    )
    with identity_owner_engine.connect() as db:
        before = db.execute(
            text("SELECT id, version, password_hash, totp_sealed FROM identity.account")
        ).one()
    client = _client(identity_rw_engine, dependencies)
    response = client.post(
        f"/api/v1/admin/accounts/{before.id}/totp-reset",
        json={"reason": "verified lost authenticator"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "account-totp-reset-0001",
            "If-Match": f'"v{before.version}"',
        },
    )

    assert response.status_code == 204
    assert response.content == b""
    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=old_session, dependencies=dependencies) is None
        bootstrap = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="totp-reset-test",
            dependencies=dependencies,
        )
    assert isinstance(bootstrap, IssuedSession)
    assert bootstrap.bootstrap_purpose is BootstrapPurpose.INITIAL_SETUP
    with identity_rw_engine.begin() as db:
        enrollment = enroll_totp(
            db,
            bootstrap_token=bootstrap.raw_token,
            dependencies=dependencies,
        )
    assert not isinstance(enrollment, BootstrapDenial)
    assert enrollment.secret != old_secret
    with identity_owner_engine.connect() as db:
        after = db.execute(
            text(
                "SELECT status, version, password_hash, totp_sealed, "
                "totp_confirmed_at, totp_last_step FROM identity.account"
            )
        ).one()
        audit_rows = db.execute(
            text("SELECT action, reason FROM audit.audit_event ORDER BY occurred_at, id")
        ).all()
    assert after.status == "PENDING_INIT"
    assert after.version == before.version + 2  # reset, then new enrollment
    assert after.password_hash == before.password_hash
    assert after.totp_sealed is not None and after.totp_sealed != before.totp_sealed
    assert after.totp_confirmed_at is None
    assert after.totp_last_step is None
    actions = {row.action for row in audit_rows}
    assert {"identity.totp.reset", "identity.sessions.revoked"} <= actions
    totp_audit = next(row for row in audit_rows if row.action == "identity.totp.reset")
    assert (
        f"verified lost authenticator; beforeVersion={before.version}; "
        f"afterVersion={before.version + 1}"
    ) == totp_audit.reason


def test_disable_converges_after_owner_commit_and_replay_is_exactly_once(
    clean_identity_db: None,
    clean_admin_convergence: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    admin_authorization_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dependencies = identity_dependencies()
    _initialize_account(identity_rw_engine, setup_dependencies, monkeypatch)
    with identity_owner_engine.connect() as db:
        account = db.execute(text("SELECT id, version FROM identity.account")).one()

    recomputes: list[tuple[str, ...]] = []
    orchestrator = SecurityChangeOrchestrator(
        admin_authorization_engine,
        authorization_dependencies(),
        recompute_membership=lambda account_ids: recomputes.append(account_ids),
    )
    monkeypatch.setattr(bootstrap, "security_change_orchestrator", lambda: orchestrator)
    dependencies = replace(
        setup_dependencies,
        on_auth_change=bootstrap._identity_authorization_change,
    )
    client = _client(
        identity_rw_engine,
        dependencies,
        security_changes=orchestrator,
    )
    headers = {
        **SAME_ORIGIN,
        "Idempotency-Key": "account-disable-convergence-0001",
        "If-Match": f'"v{account.version}"',
    }
    path = f"/api/v1/admin/accounts/{account.id}/disable"

    first = client.post(path, json={"reason": "remove access now"}, headers=headers)
    replay = client.post(path, json={"reason": "remove access now"}, headers=headers)

    assert first.status_code == replay.status_code == 204
    assert first.headers["etag"] == replay.headers["etag"] == f'"v{account.version + 1}"'
    with identity_owner_engine.connect() as db:
        work = db.execute(
            text(
                "SELECT status, source_transaction_id, idempotency_claim_id "
                'FROM "authorization".convergence_work '
                "WHERE source_module='identity' AND operation='disable' "
                "AND idempotency_key='account-disable-convergence-0001'"
            )
        ).one()
        version = db.execute(
            text(
                'SELECT version, dirty_generation FROM "authorization".principal_version '
                "WHERE account_id=:account_id"
            ),
            {"account_id": str(account.id)},
        ).one()
        facts = db.execute(
            text(
                "SELECT (SELECT status FROM identity.account WHERE id=:account_id), "
                "(SELECT count(*) FROM identity.idempotency_record "
                " WHERE idempotency_key='account-disable-convergence-0001'), "
                '(SELECT count(*) FROM "authorization".convergence_work '
                " WHERE idempotency_key='account-disable-convergence-0001')"
            ),
            {"account_id": str(account.id)},
        ).one()
        convergence_audits = db.execute(
            text(
                "SELECT actor, target_id, reason FROM audit.audit_event "
                "WHERE action='authorization.identity.converged'"
            )
        ).all()
    assert work.status == "COMPLETED"
    assert work.source_transaction_id is not None
    assert work.idempotency_claim_id is not None
    assert version == (2, None)
    assert facts == ("DISABLED", 1, 1)
    assert recomputes == [(str(account.id),)]
    assert len(convergence_audits) == 1
    assert convergence_audits[0].actor == "00000000-0000-0000-0000-000000000013"
    assert convergence_audits[0].target_id == str(account.id)
    assert (
        "sourceOperation=disable; beforeAuthorizationVersion=1; "
        "afterAuthorizationVersion=2; authorizationVersion=2" == convergence_audits[0].reason
    )


def test_disable_replay_stays_unavailable_until_persisted_convergence_recovers(
    clean_identity_db: None,
    clean_admin_convergence: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    admin_authorization_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dependencies = identity_dependencies()
    _initialize_account(identity_rw_engine, setup_dependencies, monkeypatch)
    with identity_owner_engine.connect() as db:
        account = db.execute(text("SELECT id, version FROM identity.account")).one()

    attempts: list[tuple[str, ...]] = []

    def unavailable(account_ids: tuple[str, ...]) -> None:
        attempts.append(account_ids)
        raise RuntimeError("projection unavailable")

    authorization_deps = authorization_dependencies()
    failing = SecurityChangeOrchestrator(
        admin_authorization_engine,
        authorization_deps,
        recompute_membership=unavailable,
    )
    monkeypatch.setattr(bootstrap, "security_change_orchestrator", lambda: failing)
    dependencies = replace(
        setup_dependencies,
        on_auth_change=bootstrap._identity_authorization_change,
    )
    client = _client(identity_rw_engine, dependencies, security_changes=failing)
    headers = {
        **SAME_ORIGIN,
        "Idempotency-Key": "account-disable-convergence-recovery-0001",
        "If-Match": f'"v{account.version}"',
    }
    path = f"/api/v1/admin/accounts/{account.id}/disable"
    body = {"reason": "remove access despite projection outage"}

    first = client.post(path, json=body, headers=headers)
    unavailable_replay = client.post(path, json=body, headers=headers)

    assert first.status_code == unavailable_replay.status_code == 503
    with identity_owner_engine.connect() as db:
        pending = db.execute(
            text(
                'SELECT status FROM "authorization".convergence_work '
                "WHERE idempotency_key='account-disable-convergence-recovery-0001'"
            )
        ).scalar_one()
        fact = db.execute(
            text("SELECT status, version FROM identity.account WHERE id=:account_id"),
            {"account_id": str(account.id)},
        ).one()
    assert pending == "PENDING"
    assert fact == ("DISABLED", account.version + 1)

    recovered = SecurityChangeOrchestrator(
        admin_authorization_engine,
        authorization_deps,
        recompute_membership=lambda account_ids: attempts.append(account_ids),
    )
    assert recovered.reconcile_pending() is True
    replay = client.post(path, json=body, headers=headers)

    assert replay.status_code == 204
    assert replay.headers["etag"] == f'"v{account.version + 1}"'
    with identity_owner_engine.connect() as db:
        state = db.execute(
            text(
                'SELECT version, dirty_generation FROM "authorization".principal_version '
                "WHERE account_id=:account_id"
            ),
            {"account_id": str(account.id)},
        ).one()
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM identity.idempotency_record "
                " WHERE idempotency_key='account-disable-convergence-recovery-0001'), "
                '(SELECT count(*) FROM "authorization".convergence_work '
                " WHERE idempotency_key='account-disable-convergence-recovery-0001')"
            )
        ).one()
    assert state == (2, None)
    assert counts == (1, 1)
    assert attempts == [(str(account.id),), (str(account.id),)]


def test_unexpected_failure_rolls_back_fact_audit_and_idempotency_claim(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    base = identity_dependencies()

    class FailingAudit:
        def append_in_transaction(self, db: object, envelope: object) -> None:
            del db, envelope
            raise RuntimeError("injected audit failure with no credential")

    dependencies = replace(base, audit=FailingAudit())
    client = _client(identity_rw_engine, dependencies)
    response = client.post(
        "/api/v1/admin/accounts",
        json={
            "employeeNo": "00000001",
            "displayName": "Alice",
            "reason": "must rollback",
        },
        headers={**SAME_ORIGIN, "Idempotency-Key": "account-rollback-0001"},
    )

    assert response.status_code == 500
    assert "audit" not in response.text.lower()
    with identity_owner_engine.connect() as db:
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM identity.account), "
                "(SELECT count(*) FROM identity.temp_credential), "
                "(SELECT count(*) FROM identity.idempotency_record), "
                "(SELECT count(*) FROM audit.audit_event)"
            )
        ).one()
    assert counts == (0, 0, 0, 0)
