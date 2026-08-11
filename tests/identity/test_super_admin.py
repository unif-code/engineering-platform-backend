import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from types import SimpleNamespace

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import control_plane.app.modules.identity as identity
from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.identity.api.auth_routes import IdentityHttpRuntime
from control_plane.app.modules.identity.api.super_admin_routes import (
    create_super_admin_router,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.identity.test_auth_flow import _initialize_account

pytestmark = pytest.mark.integration


@dataclass
class _AuthChangeRecorder:
    account_ids: list[str]

    def __call__(self, account_id: str) -> None:
        self.account_ids.append(account_id)


def test_super_admin_http_contract_has_fixed_operations_and_write_preflight() -> None:
    app = create_app()
    schema = app.openapi()

    list_operation = schema["paths"]["/api/v1/admin/super-admins"]["get"]
    add_operation = schema["paths"]["/api/v1/admin/super-admins"]["post"]
    remove_operation = schema["paths"]["/api/v1/admin/super-admins/{id}"]["delete"]
    assert list_operation["operationId"] == "super_admin_list"
    assert add_operation["operationId"] == "super_admin_add"
    assert remove_operation["operationId"] == "super_admin_remove"
    for operation in (add_operation, remove_operation):
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert parameters["Idempotency-Key"]["required"] is True
        assert parameters["If-Match"]["required"] is True
        assert set(operation["responses"]) >= {
            "200",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
            "503",
        }
        etag = operation["responses"]["200"]["headers"]["ETag"]
        assert etag["schema"]["type"] == "string"
        assert "version" in etag["description"].lower()

    client = TestClient(app, base_url="https://testserver")
    for method, path, body in (
        (
            "post",
            "/api/v1/admin/super-admins",
            {"accountId": "target", "totpCode": "000000", "reason": "approved"},
        ),
        (
            "delete",
            "/api/v1/admin/super-admins/target",
            {"totpCode": "000000", "reason": "approved"},
        ),
    ):
        response = client.request(
            method,
            path,
            json=body,
            headers={
                "Idempotency-Key": "task10-csrf-0001",
                "If-Match": '"v1"',
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
                "X-Request-ID": "req-task10csrf",
            },
        )
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["requestId"] == "req-task10csrf"


def test_super_admin_api_requires_fresh_totp_reason_and_durable_idempotency(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _AuthChangeRecorder([])
    dependencies = replace(identity_dependencies(), on_auth_change=recorder)
    actor_secret, _actor_token = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000001",
        display_name="Alice",
    )
    _target_secret, _target_token = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000002",
        display_name="Bob",
    )
    with identity_owner_engine.begin() as db:
        actor_id = str(
            db.execute(
                text(
                    "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                    "WHERE employee_no='00000001' RETURNING id"
                )
            ).scalar_one()
        )
        target = db.execute(
            text("SELECT id, version FROM identity.account WHERE employee_no='00000002'")
        ).one()
    recorder.account_ids.clear()
    guarded: list[str] = []
    runtime = IdentityHttpRuntime(identity_rw_engine, dependencies)
    principal = SimpleNamespace(account_id=actor_id)
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_super_admin_router(
            lambda: runtime,
            lambda: principal,
            lambda _principal, capability, _scope: guarded.append(capability),
        )
    )
    client = TestClient(
        app,
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    headers = {
        "Idempotency-Key": "task10-add-admin-0001",
        "If-Match": f'"v{target.version}"',
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "X-Request-ID": "req-task10add",
    }
    dependencies.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
    totp_code = pyotp.TOTP(actor_secret).at(dependencies.clock.now())
    add_body = {
        "accountId": str(target.id),
        "totpCode": totp_code,
        "reason": "secondary recovery administrator",
    }

    added = client.post("/api/v1/admin/super-admins", json=add_body, headers=headers)
    replayed = client.post("/api/v1/admin/super-admins", json=add_body, headers=headers)

    assert added.status_code == 200
    assert added.json()["isSuperAdmin"] is True
    assert added.headers["etag"] == f'"v{target.version + 1}"'
    assert replayed.status_code == 200
    assert replayed.json() == added.json()
    assert recorder.account_ids == [str(target.id)]
    assert guarded == [
        "platform.super_admin.manage",
        "platform.super_admin.manage",
    ]

    replay_totp = client.post(
        "/api/v1/admin/super-admins",
        json=add_body,
        headers={**headers, "Idempotency-Key": "task10-add-admin-0002"},
    )
    assert replay_totp.status_code == 403
    assert replay_totp.headers["content-type"].startswith("application/problem+json")
    assert replay_totp.json()["requestId"] == "req-task10add"

    blank_reason = client.request(
        "DELETE",
        f"/api/v1/admin/super-admins/{target.id}",
        json={"totpCode": "000000", "reason": "   "},
        headers={
            **headers,
            "Idempotency-Key": "task10-remove-admin-0001",
            "If-Match": added.headers["etag"],
            "X-Request-ID": "req-task10reason",
        },
    )
    assert blank_reason.status_code == 422
    assert blank_reason.json()["requestId"] == "req-task10reason"

    dependencies.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
    stale = client.request(
        "DELETE",
        f"/api/v1/admin/super-admins/{target.id}",
        json={
            "totpCode": pyotp.TOTP(actor_secret).at(dependencies.clock.now()),
            "reason": "stale governance denial proof",
        },
        headers={
            **headers,
            "Idempotency-Key": "task10-remove-admin-0002",
            "If-Match": f'"v{target.version}"',
            "X-Request-ID": "req-task10stale",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["requestId"] == "req-task10stale"
    with identity_owner_engine.connect() as db:
        denial_audits = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='identity.super_admin.removed' AND target_id=:target_id "
                "AND result='DENIED'"
            ),
            {"target_id": str(target.id)},
        ).scalar_one()
    assert denial_audits == 1

    dependencies.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
    removed = client.request(
        "DELETE",
        f"/api/v1/admin/super-admins/{target.id}",
        json={
            "totpCode": pyotp.TOTP(actor_secret).at(dependencies.clock.now()),
            "reason": "remove secondary recovery administrator",
        },
        headers={
            **headers,
            "Idempotency-Key": "task10-remove-admin-0003",
            "If-Match": added.headers["etag"],
        },
    )
    assert removed.status_code == 200
    assert removed.json()["isSuperAdmin"] is False
    assert recorder.account_ids == [str(target.id), str(target.id)]

    listed = client.get("/api/v1/admin/super-admins")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [actor_id]
    assert listed.json()["nextCursor"] is None
    with identity_owner_engine.connect() as db:
        evidence = (
            db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM identity.idempotency_record "
                    "WHERE operation IN ('super_admin_add','super_admin_remove')) AS claims, "
                    "(SELECT count(*) FROM identity.auth_challenge "
                    "WHERE purpose IN ('SUPER_ADMIN_ADD','SUPER_ADMIN_REMOVE') "
                    "AND consumed_at IS NOT NULL) AS consumed"
                )
            )
            .mappings()
            .one()
        )
    assert evidence["claims"] == 4
    assert evidence["consumed"] == 3


def test_super_admin_http_totp_attempt_scope_caps_distinct_commands(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    actor_secret, _actor_token = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000001",
        display_name="Alice",
    )
    _target_secret, _target_token = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000002",
        display_name="Bob",
    )
    with identity_owner_engine.begin() as db:
        actor_id = str(
            db.execute(
                text(
                    "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                    "WHERE employee_no='00000001' RETURNING id"
                )
            ).scalar_one()
        )
        target = db.execute(
            text("SELECT id, version FROM identity.account WHERE employee_no='00000002'")
        ).one()
    runtime = IdentityHttpRuntime(identity_rw_engine, dependencies)
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_super_admin_router(
            lambda: runtime,
            lambda: SimpleNamespace(account_id=actor_id),
            lambda _principal, _capability, _scope: None,
        )
    )
    client = TestClient(app, base_url="https://testserver")
    with identity_rw_engine.connect() as db:
        policy = dependencies.policy.get_identity_policy(db)
    valid_code = pyotp.TOTP(actor_secret).at(dependencies.clock.now())
    wrong_code = f"{(int(valid_code) + 1) % 1_000_000:06d}"

    for attempt in range(policy.totp_attempt_cap + 1):
        response = client.post(
            "/api/v1/admin/super-admins",
            json={
                "accountId": str(target.id),
                "totpCode": wrong_code,
                "reason": "attempt cap proof",
            },
            headers={
                "Idempotency-Key": f"task10-attempt-cap-{attempt:04d}",
                "If-Match": f'"v{target.version}"',
                "Origin": "https://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert response.status_code == 403

    with identity_owner_engine.connect() as db:
        challenge_count, attempt_count, target_is_admin = db.execute(
            text(
                "SELECT count(*), coalesce(sum(attempt_count), 0), "
                "(SELECT is_super_admin FROM identity.account WHERE id=:target_id) "
                "FROM identity.auth_challenge WHERE actor_id=:actor_id "
                "AND purpose='SUPER_ADMIN_ADD'"
            ),
            {"actor_id": actor_id, "target_id": str(target.id)},
        ).one()
    assert challenge_count == policy.totp_attempt_cap
    assert attempt_count == policy.totp_attempt_cap
    assert target_is_admin is False


def test_environment_bootstrap_is_once_only_atomic_and_credential_safe(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    recorder = _AuthChangeRecorder([])
    dependencies = replace(
        identity_dependencies(),
        on_auth_change=recorder,
    )
    with identity_rw_engine.begin() as db:
        account, temporary_password = identity.bootstrap_super_admin(
            db,
            employee_no="00000001",
            display_name="张三",
            dependencies=dependencies,
        )

    assert account.employee_no == "00000001"
    assert account.display_name == "张三"
    assert account.is_super_admin is True
    assert account.status is identity.AccountStatus.PENDING_INIT
    assert recorder.account_ids == [account.id]

    with identity_owner_engine.connect() as db:
        persisted = (
            db.execute(
                text(
                    "SELECT a.employee_no, a.display_name, a.is_super_admin, "
                    "t.secret_hash, t.expires_at, t.created_at "
                    "FROM identity.account a JOIN identity.temp_credential t "
                    "ON t.account_id=a.id"
                )
            )
            .mappings()
            .one()
        )
        audit_rows = (
            db.execute(
                text(
                    "SELECT actor, actor_type, action, target_id, result, reason "
                    "FROM audit.audit_event ORDER BY occurred_at, id"
                )
            )
            .mappings()
            .all()
        )
    assert persisted["is_super_admin"] is True
    assert persisted["secret_hash"] != temporary_password
    assert persisted["expires_at"] > persisted["created_at"]
    assert audit_rows[-1]["actor"] == "SYSTEM_BOOTSTRAP"
    assert audit_rows[-1]["actor_type"] == "SYSTEM"
    assert audit_rows[-1]["action"] == "identity.super_admin.bootstrapped"
    assert audit_rows[-1]["target_id"] == account.id
    assert audit_rows[-1]["result"] == "SUCCESS"
    assert temporary_password not in repr(audit_rows)

    with identity_rw_engine.begin() as db:
        with pytest.raises(identity.SuperAdminBootstrapConflict):
            identity.bootstrap_super_admin(
                db,
                employee_no="00000002",
                display_name="李四",
                dependencies=dependencies,
            )
    with identity_owner_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity.account")).scalar_one() == 1
        assert (
            db.execute(
                text("SELECT count(*) FROM identity.account WHERE is_super_admin=true")
            ).scalar_one()
            == 1
        )
        assert db.execute(text("SELECT count(*) FROM audit.audit_event")).scalar_one() == 3


def test_add_remove_require_fresh_purpose_bound_totp_revoke_sessions_and_keep_last_admin(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _AuthChangeRecorder([])
    dependencies = replace(identity_dependencies(), on_auth_change=recorder)
    actor_secret, actor_token = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000001",
        display_name="Alice",
    )
    _target_secret, target_token = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000002",
        display_name="Bob",
    )
    with identity_owner_engine.begin() as db:
        actor_row = db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                "WHERE employee_no='00000001' RETURNING id, version"
            )
        ).one()
        actor_id = str(actor_row.id)
        actor_version = int(actor_row.version)
        target_row = db.execute(
            text("SELECT id, version FROM identity.account WHERE employee_no='00000002'")
        ).one()
        target_id = str(target_row.id)
        target_version = int(target_row.version)
    recorder.account_ids.clear()
    dependencies.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
    add_code = pyotp.TOTP(actor_secret).at(dependencies.clock.now())
    with identity_rw_engine.begin() as db:
        add_challenge = identity.issue_super_admin_challenge(
            db,
            actor_account_id=actor_id,
            operation="ADD",
            dependencies=dependencies,
        )
    with identity_rw_engine.begin() as db:
        added = identity.add_super_admin(
            db,
            target_account_id=target_id,
            actor_account_id=actor_id,
            challenge_token=add_challenge,
            totp_code=add_code,
            reason="secondary recovery administrator",
            expected_version=target_version,
            dependencies=dependencies,
        )
    assert added.is_super_admin is True
    assert recorder.account_ids == [target_id]
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT revoked_at IS NOT NULL FROM identity.session "
                    "WHERE token_hash=:token_hash"
                ),
                {"token_hash": hashlib.sha256(target_token.encode()).hexdigest()},
            ).scalar_one()
            is True
        )
        assert (
            db.execute(
                text(
                    "SELECT consumed_at IS NOT NULL FROM identity.auth_challenge "
                    "WHERE purpose='SUPER_ADMIN_ADD'"
                )
            ).scalar_one()
            is True
        )

    with identity_rw_engine.begin() as db:
        with pytest.raises(identity.TotpChallengeFailed):
            identity.remove_super_admin(
                db,
                target_account_id=target_id,
                actor_account_id=actor_id,
                challenge_token=add_challenge,
                totp_code=add_code,
                reason="replayed challenge must fail",
                expected_version=added.version,
                dependencies=dependencies,
            )

    dependencies.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
    remove_code = pyotp.TOTP(actor_secret).at(dependencies.clock.now())
    with identity_rw_engine.begin() as db:
        remove_challenge = identity.issue_super_admin_challenge(
            db,
            actor_account_id=actor_id,
            operation="REMOVE",
            dependencies=dependencies,
        )
    with identity_rw_engine.begin() as db:
        removed = identity.remove_super_admin(
            db,
            target_account_id=target_id,
            actor_account_id=actor_id,
            challenge_token=remove_challenge,
            totp_code=remove_code,
            reason="remove secondary recovery administrator",
            expected_version=added.version,
            dependencies=dependencies,
        )
    assert removed.is_super_admin is False
    assert recorder.account_ids == [target_id, target_id]

    dependencies.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
    last_code = pyotp.TOTP(actor_secret).at(dependencies.clock.now())
    with identity_rw_engine.begin() as db:
        last_challenge = identity.issue_super_admin_challenge(
            db,
            actor_account_id=actor_id,
            operation="REMOVE",
            dependencies=dependencies,
        )
    with identity_rw_engine.begin() as db:
        with pytest.raises(identity.LastEffectiveSuperAdmin):
            identity.remove_super_admin(
                db,
                target_account_id=actor_id,
                actor_account_id=actor_id,
                challenge_token=last_challenge,
                totp_code=last_code,
                reason="must not remove the last Super Admin",
                expected_version=actor_version,
                dependencies=dependencies,
            )
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text("SELECT is_super_admin FROM identity.account WHERE id=:id"),
                {"id": actor_id},
            ).scalar_one()
            is True
        )
        assert (
            db.execute(
                text(
                    "SELECT revoked_at IS NULL FROM identity.session WHERE token_hash=:token_hash"
                ),
                {"token_hash": hashlib.sha256(actor_token.encode()).hexdigest()},
            ).scalar_one()
            is True
        )


def test_concurrent_self_removals_serialize_and_leave_one_effective_super_admin(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = identity_dependencies()
    first_secret, _first_token = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000001",
        display_name="Alice",
    )
    second_secret, _second_token = _initialize_account(
        identity_rw_engine,
        dependencies,
        monkeypatch,
        employee_no="00000002",
        display_name="Bob",
    )
    with identity_owner_engine.begin() as db:
        rows = db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                "RETURNING employee_no, id, version"
            )
        ).all()
    account_ids = {str(row.employee_no): str(row.id) for row in rows}
    account_versions = {str(row.employee_no): int(row.version) for row in rows}
    dependencies.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
    codes = {
        "00000001": pyotp.TOTP(first_secret).at(dependencies.clock.now()),
        "00000002": pyotp.TOTP(second_secret).at(dependencies.clock.now()),
    }
    challenges: dict[str, str] = {}
    for employee_no in ("00000001", "00000002"):
        with identity_rw_engine.begin() as db:
            challenges[employee_no] = identity.issue_super_admin_challenge(
                db,
                actor_account_id=account_ids[employee_no],
                operation="REMOVE",
                dependencies=dependencies,
            )

    ready = threading.Barrier(3)

    def remove_self(employee_no: str) -> object:
        try:
            with identity_rw_engine.begin() as db:
                ready.wait(timeout=5)
                return identity.remove_super_admin(
                    db,
                    target_account_id=account_ids[employee_no],
                    actor_account_id=account_ids[employee_no],
                    challenge_token=challenges[employee_no],
                    totp_code=codes[employee_no],
                    reason=f"concurrent removal {employee_no}",
                    expected_version=account_versions[employee_no],
                    dependencies=dependencies,
                )
        except Exception as exc:  # return the independently committed outcome
            return exc

    blocker = identity_owner_engine.connect()
    transaction = blocker.begin()
    try:
        blocker.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('identity.effective_super_admin', 0))"
            )
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(remove_self, value) for value in account_ids]
            ready.wait(timeout=5)
            time.sleep(0.15)
            assert all(not future.done() for future in futures)
            transaction.commit()
            outcomes = [future.result(timeout=5) for future in futures]
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()

    assert sum(isinstance(value, identity.AccountDto) for value in outcomes) == 1
    assert sum(isinstance(value, identity.LastEffectiveSuperAdmin) for value in outcomes) == 1
    with identity_owner_engine.connect() as db:
        effective = db.execute(
            text(
                "SELECT count(*) FROM identity.account WHERE is_super_admin=true "
                "AND status='ENABLED' AND password_hash IS NOT NULL "
                "AND totp_confirmed_at IS NOT NULL"
            )
        ).scalar_one()
    assert effective == 1
