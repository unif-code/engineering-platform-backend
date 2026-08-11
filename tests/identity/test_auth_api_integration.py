import threading
from dataclasses import replace
from datetime import timedelta
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, text

from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.identity import (
    EffectiveIdentityPolicy,
    IdentityDependencies,
    Principal,
    SessionKind,
    create_account,
    issue_temp_password,
    validate_session,
)
from control_plane.app.modules.identity.api.auth_routes import IdentityHttpRuntime
from tests.identity.task5_helpers import MutableClock, dependencies

pytestmark = pytest.mark.integration

SYSTEM = Principal(employee_id="SYSTEM", name="System")
VALID_PASSWORD = "Str0ng!Secure#2026"
NEW_PASSWORD = "An0ther!Secure#2026"


class ExpiringPasswordPolicy:
    def get_identity_policy(self, db: object) -> EffectiveIdentityPolicy:
        del db
        return EffectiveIdentityPolicy(password_max_age=timedelta(days=90))


def _client(identity_rw_engine: Engine) -> tuple[TestClient, IdentityDependencies]:
    deps = dependencies()
    runtime = IdentityHttpRuntime(engine=identity_rw_engine, dependencies=deps)
    return (
        TestClient(
            create_app(identity_runtime_provider=lambda: runtime),
            base_url="https://testserver",
        ),
        deps,
    )


def _create_account(identity_rw_engine: Engine, deps: IdentityDependencies) -> str:
    with identity_rw_engine.begin() as db:
        _, temporary_password = create_account(
            db,
            employee_no="00000001",
            display_name="Alice",
            actor=SYSTEM,
            reason="api integration",
            dependencies=deps,
        )
    return temporary_password


def _clock(deps: IdentityDependencies) -> MutableClock:
    return cast(MutableClock, deps.clock)


def _initialize_via_api(
    identity_rw_engine: Engine,
    client: TestClient,
    deps: IdentityDependencies,
    monkeypatch: pytest.MonkeyPatch,
    *,
    key_prefix: str,
) -> tuple[str, str]:
    temporary_password = _create_account(identity_rw_engine, deps)
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"employeeNo": "00000001", "password": temporary_password},
            headers={"Idempotency-Key": f"{key_prefix}-login"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/auth/bootstrap/password",
            json={"password": VALID_PASSWORD},
            headers={"Idempotency-Key": f"{key_prefix}-password"},
        ).status_code
        == 200
    )
    enrollment = client.post(
        "/api/v1/auth/bootstrap/totp/enroll",
        headers={"Idempotency-Key": f"{key_prefix}-enroll"},
    )
    assert enrollment.status_code == 200
    secret = parse_qs(urlsplit(enrollment.json()["provisioningUri"]).query)["secret"][0]
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: deps.clock.now().timestamp(),
    )
    confirmation = client.post(
        "/api/v1/auth/bootstrap/totp/confirm",
        json={"code": pyotp.TOTP(secret).at(deps.clock.now())},
        headers={"Idempotency-Key": f"{key_prefix}-confirm"},
    )
    assert confirmation.status_code == 200
    full_token = client.cookies.get("ep_session")
    assert full_token
    return secret, full_token


def test_bootstrap_login_replays_the_same_cookie_without_reconsuming_credential(
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    clean_identity_db: None,
) -> None:
    client, deps = _client(identity_rw_engine)
    temporary_password = _create_account(identity_rw_engine, deps)
    request = {"employeeNo": "00000001", "password": temporary_password}
    headers = {"Idempotency-Key": "bootstrap-login-replay"}

    first = client.post("/api/v1/auth/login", json=request, headers=headers)
    second = client.post("/api/v1/auth/login", json=request, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"state": "BOOTSTRAP_REQUIRED"}
    assert first.headers["set-cookie"] == second.headers["set-cookie"]
    assert "Secure" in first.headers["set-cookie"]
    assert "HttpOnly" in first.headers["set-cookie"]
    assert "SameSite=lax" in first.headers["set-cookie"]
    assert "Path=/" in first.headers["set-cookie"]
    assert "ep_session" not in str(first.json())
    with identity_rw_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity.session")).scalar_one() == 1
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='identity.temp_credential.consumed'"
                )
            ).scalar_one()
            == 1
        )


def test_full_bootstrap_api_sets_full_cookie_then_logout_revokes_it(
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    clean_identity_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, deps = _client(identity_rw_engine)
    temporary_password = _create_account(identity_rw_engine, deps)

    login = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": temporary_password},
        headers={"Idempotency-Key": "bootstrap-login-flow"},
    )
    assert login.status_code == 200
    bootstrap_token = client.cookies.get("ep_session")
    assert bootstrap_token

    password = client.post(
        "/api/v1/auth/bootstrap/password",
        json={"password": VALID_PASSWORD},
        headers={"Idempotency-Key": "bootstrap-password-flow"},
    )
    assert password.status_code == 200
    assert password.json() == {"state": "PASSWORD_SET"}
    password_replay = client.post(
        "/api/v1/auth/bootstrap/password",
        json={"password": VALID_PASSWORD},
        headers={"Idempotency-Key": "bootstrap-password-flow"},
    )
    assert password_replay.status_code == 200
    assert password_replay.json() == password.json()

    enrollment = client.post(
        "/api/v1/auth/bootstrap/totp/enroll",
        headers={"Idempotency-Key": "bootstrap-enroll-flow"},
    )
    assert enrollment.status_code == 200
    provisioning_uri = enrollment.json()["provisioningUri"]
    assert provisioning_uri.startswith("otpauth://")
    assert "secret" not in enrollment.json()
    enrollment_replay = client.post(
        "/api/v1/auth/bootstrap/totp/enroll",
        headers={"Idempotency-Key": "bootstrap-enroll-flow"},
    )
    assert enrollment_replay.status_code == 200
    assert enrollment_replay.json() == enrollment.json()
    secret = parse_qs(urlsplit(provisioning_uri).query)["secret"][0]
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: deps.clock.now().timestamp(),
    )

    confirmation = client.post(
        "/api/v1/auth/bootstrap/totp/confirm",
        json={"code": pyotp.TOTP(secret).at(deps.clock.now())},
        headers={"Idempotency-Key": "bootstrap-confirm-flow"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json() == {"state": "AUTHENTICATED"}
    full_token = client.cookies.get("ep_session")
    assert full_token and full_token != bootstrap_token
    confirmation_retry_client = TestClient(client.app, base_url="https://testserver")
    confirmation_retry_client.cookies.set("ep_session", bootstrap_token)
    confirmation_replay = confirmation_retry_client.post(
        "/api/v1/auth/bootstrap/totp/confirm",
        json={"code": pyotp.TOTP(secret).at(deps.clock.now())},
        headers={"Idempotency-Key": "bootstrap-confirm-flow"},
    )
    assert confirmation_replay.status_code == 200
    assert confirmation_replay.headers["set-cookie"] == confirmation.headers["set-cookie"]
    with identity_rw_engine.begin() as db:
        principal = validate_session(
            db,
            raw_token=full_token,
            dependencies=deps,
        )
    assert principal is not None
    assert principal.session_kind is SessionKind.FULL

    logged_out = client.post(
        "/api/v1/auth/logout",
        headers={"Idempotency-Key": "bootstrap-logout-flow"},
    )
    assert logged_out.status_code == 200
    assert logged_out.json() == {"state": "LOGGED_OUT"}
    assert "Max-Age=0" in logged_out.headers["set-cookie"]
    logout_retry_client = TestClient(client.app, base_url="https://testserver")
    logout_retry_client.cookies.set("ep_session", full_token)
    logout_replay = logout_retry_client.post(
        "/api/v1/auth/logout",
        headers={"Idempotency-Key": "bootstrap-logout-flow"},
    )
    assert logout_replay.status_code == 200
    assert logout_replay.headers["set-cookie"] == logged_out.headers["set-cookie"]
    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=full_token, dependencies=deps) is None

    _clock(deps).value += timedelta(seconds=30)
    normal_login = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": VALID_PASSWORD},
        headers={"Idempotency-Key": "normal-login-flow"},
    )
    assert normal_login.status_code == 200
    assert normal_login.json()["state"] == "TOTP_REQUIRED"
    challenge_token = normal_login.json()["challengeToken"]
    normal_login_replay = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": VALID_PASSWORD},
        headers={"Idempotency-Key": "normal-login-flow"},
    )
    assert normal_login_replay.json() == normal_login.json()

    normal_totp = client.post(
        "/api/v1/auth/totp",
        json={
            "challengeToken": challenge_token,
            "code": pyotp.TOTP(secret).at(deps.clock.now()),
        },
        headers={"Idempotency-Key": "normal-totp-flow"},
    )
    assert normal_totp.status_code == 200
    assert normal_totp.json() == {"state": "AUTHENTICATED"}
    totp_retry_client = TestClient(client.app, base_url="https://testserver")
    normal_totp_replay = totp_retry_client.post(
        "/api/v1/auth/totp",
        json={
            "challengeToken": challenge_token,
            "code": pyotp.TOTP(secret).at(deps.clock.now()),
        },
        headers={"Idempotency-Key": "normal-totp-flow"},
    )
    assert normal_totp_replay.status_code == 200
    assert normal_totp_replay.headers["set-cookie"] == normal_totp.headers["set-cookie"]
    normal_cookie = normal_totp.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    with identity_rw_engine.connect() as db:
        persisted = (
            db.execute(
                text(
                    "SELECT a.totp_sealed, c.token_hash AS challenge_hash, "
                    "s.token_hash AS session_hash FROM identity.account a "
                    "JOIN identity.auth_challenge c ON c.account_id=a.id "
                    "JOIN identity.session s ON s.account_id=a.id AND s.kind='FULL' "
                    "WHERE c.token_hash IS NOT NULL ORDER BY s.created_at DESC LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
    assert secret.encode() not in persisted["totp_sealed"]
    assert challenge_token not in persisted["challenge_hash"]
    assert normal_cookie not in persisted["session_hash"]
    with identity_owner_engine.connect() as db:
        audit_text = db.execute(
            text("SELECT coalesce(string_agg(to_jsonb(e)::text, ''), '') FROM audit.audit_event e")
        ).scalar_one()
    for credential in (secret, challenge_token, bootstrap_token, full_token, normal_cookie):
        assert credential not in audit_text


def test_denial_replay_uses_current_request_id_without_extra_backoff_or_audit(
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    clean_identity_db: None,
) -> None:
    client, deps = _client(identity_rw_engine)
    _create_account(identity_rw_engine, deps)
    request = {"employeeNo": "00000001", "password": "wrong-password"}
    key = "denial-replay-key"

    first = client.post(
        "/api/v1/auth/login",
        json=request,
        headers={"Idempotency-Key": key, "X-Request-ID": "req-first"},
    )
    second = client.post(
        "/api/v1/auth/login",
        json=request,
        headers={"Idempotency-Key": key, "X-Request-ID": "req-second"},
    )

    assert first.status_code == second.status_code == 401
    assert first.json()["requestId"] == "req-first"
    assert second.json()["requestId"] == "req-second"
    assert {k: v for k, v in first.json().items() if k != "requestId"} == {
        k: v for k, v in second.json().items() if k != "requestId"
    }
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT failure_count FROM identity.login_backoff WHERE employee_no='00000001'"
                )
            ).scalar_one()
            == 1
        )
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='identity.login.password' AND result='DENIED'"
                )
            ).scalar_one()
            == 1
        )

    conflict = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": "different-password"},
        headers={"Idempotency-Key": key, "X-Request-ID": "req-conflict"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["requestId"] == "req-conflict"


def test_concurrent_bootstrap_login_executes_once_and_seals_cookie(
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    clean_identity_db: None,
) -> None:
    first_client, deps = _client(identity_rw_engine)
    second_client = TestClient(first_client.app, base_url="https://testserver")
    temporary_password = _create_account(identity_rw_engine, deps)
    barrier = threading.Barrier(2)
    responses: list[Response] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def worker(client: TestClient) -> None:
        try:
            barrier.wait(timeout=5)
            response = client.post(
                "/api/v1/auth/login",
                json={"employeeNo": "00000001", "password": temporary_password},
                headers={"Idempotency-Key": "concurrent-api-login"},
            )
            with lock:
                responses.append(response)
        except BaseException as exc:
            with lock:
                failures.append(exc)

    threads = [
        threading.Thread(target=worker, args=(first_client,)),
        threading.Thread(target=worker, args=(second_client,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert len(responses) == 2
    assert {response.status_code for response in responses} == {200}
    assert len({response.headers["set-cookie"] for response in responses}) == 1
    cookie = next(iter(responses)).headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    with identity_rw_engine.connect() as db:
        stored = (
            db.execute(
                text(
                    "SELECT result_metadata, sealed_response FROM identity.idempotency_record "
                    "WHERE operation='auth_login'"
                )
            )
            .mappings()
            .one()
        )
        assert db.execute(text("SELECT count(*) FROM identity.session")).scalar_one() == 1
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='identity.temp_credential.consumed'"
                )
            ).scalar_one()
            == 1
        )
    assert stored["result_metadata"] == {"kind": "http-response", "schemaVersion": 1}
    assert temporary_password.encode() not in stored["sealed_response"]
    assert cookie.encode() not in stored["sealed_response"]


def test_login_backoff_and_totp_terminal_limit_return_retry_after_without_replay_effects(
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    clean_identity_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, deps = _client(identity_rw_engine)
    secret, _ = _initialize_via_api(
        identity_rw_engine,
        client,
        deps,
        monkeypatch,
        key_prefix="limitsinit",
    )
    client.cookies.clear()

    for attempt in range(5):
        denial = client.post(
            "/api/v1/auth/login",
            json={"employeeNo": "00000001", "password": "wrong-password"},
            headers={"Idempotency-Key": f"backoff-attempt-{attempt}"},
        )
        assert denial.status_code == 401
    backoff = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": "wrong-password"},
        headers={"Idempotency-Key": "backoff-attempt-5"},
    )
    assert backoff.status_code == 429
    assert backoff.headers["retry-after"] == "30"
    assert backoff.json()["retryAfter"] == 30

    with identity_rw_engine.begin() as db:
        db.execute(
            text(
                "UPDATE identity.login_backoff SET failure_count=0, "
                "last_failure_at=NULL, locked_until=NULL WHERE employee_no='00000001'"
            )
        )
    _clock(deps).value += timedelta(seconds=30)
    password_step = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": VALID_PASSWORD},
        headers={"Idempotency-Key": "terminal-password-step"},
    )
    challenge_token = password_step.json()["challengeToken"]
    valid_code = pyotp.TOTP(secret).at(deps.clock.now())
    wrong_code = "000001" if valid_code == "000000" else "000000"
    terminal: Response | None = None
    for attempt in range(5):
        terminal = client.post(
            "/api/v1/auth/totp",
            json={"challengeToken": challenge_token, "code": wrong_code},
            headers={"Idempotency-Key": f"terminal-totp-{attempt}"},
        )
        assert terminal.status_code == (429 if attempt == 4 else 401)
    assert terminal is not None
    assert terminal.headers["retry-after"] == "1"
    terminal_replay = client.post(
        "/api/v1/auth/totp",
        json={"challengeToken": challenge_token, "code": wrong_code},
        headers={"Idempotency-Key": "terminal-totp-4"},
    )
    assert terminal_replay.status_code == 429
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(text("SELECT attempt_count FROM identity.auth_challenge")).scalar_one() == 5
        )
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='identity.login.totp' AND result='DENIED'"
                )
            ).scalar_one()
            == 5
        )


def test_password_reset_bootstrap_preserves_and_revalidates_existing_totp(
    identity_rw_engine: Engine,
    clean_identity_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, deps = _client(identity_rw_engine)
    secret, _ = _initialize_via_api(
        identity_rw_engine,
        client,
        deps,
        monkeypatch,
        key_prefix="resetinit",
    )
    with identity_rw_engine.begin() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())
        temporary_password = issue_temp_password(
            db,
            account_id=account_id,
            actor=SYSTEM,
            reason="manual verification",
            dependencies=deps,
        )
    client.cookies.clear()

    login = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": temporary_password},
        headers={"Idempotency-Key": "reset-bootstrap-login"},
    )
    assert login.status_code == 200
    password = client.post(
        "/api/v1/auth/bootstrap/password",
        json={"password": NEW_PASSWORD},
        headers={"Idempotency-Key": "reset-bootstrap-password"},
    )
    assert password.status_code == 200
    forbidden_enrollment = client.post(
        "/api/v1/auth/bootstrap/totp/enroll",
        headers={"Idempotency-Key": "reset-bootstrap-enroll"},
    )
    assert forbidden_enrollment.status_code == 401
    _clock(deps).value += timedelta(seconds=30)
    confirmation = client.post(
        "/api/v1/auth/bootstrap/totp/confirm",
        json={"code": pyotp.TOTP(secret).at(deps.clock.now())},
        headers={"Idempotency-Key": "reset-bootstrap-confirm"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json() == {"state": "AUTHENTICATED"}


def test_expired_password_flow_allows_only_password_change_then_fresh_login(
    identity_rw_engine: Engine,
    clean_identity_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, deps = _client(identity_rw_engine)
    _initialize_via_api(
        identity_rw_engine,
        client,
        deps,
        monkeypatch,
        key_prefix="expiredinit",
    )
    _clock(deps).value += timedelta(days=91)
    expiring_deps = replace(deps, policy=ExpiringPasswordPolicy())
    runtime = IdentityHttpRuntime(engine=identity_rw_engine, dependencies=expiring_deps)
    expired_client = TestClient(
        create_app(identity_runtime_provider=lambda: runtime),
        base_url="https://testserver",
    )

    login = expired_client.post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": VALID_PASSWORD},
        headers={"Idempotency-Key": "expired-password-login"},
    )
    assert login.status_code == 200
    assert login.json() == {"state": "BOOTSTRAP_REQUIRED"}
    enrollment = expired_client.post(
        "/api/v1/auth/bootstrap/totp/enroll",
        headers={"Idempotency-Key": "expired-password-enroll"},
    )
    assert enrollment.status_code == 401
    changed = expired_client.post(
        "/api/v1/auth/bootstrap/password",
        json={"password": NEW_PASSWORD},
        headers={"Idempotency-Key": "expired-password-change"},
    )
    assert changed.status_code == 200
    assert changed.json() == {"state": "PASSWORD_UPDATED_LOGIN_REQUIRED"}
    assert "Max-Age=0" in changed.headers["set-cookie"]
    fresh_login = expired_client.post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": NEW_PASSWORD},
        headers={"Idempotency-Key": "expired-fresh-login"},
    )
    assert fresh_login.status_code == 200
    assert fresh_login.json()["state"] == "TOTP_REQUIRED"
