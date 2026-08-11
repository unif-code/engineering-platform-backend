from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import control_plane.app.modules.identity as identity
from control_plane.app.modules.authorization import SecurityChangeOrchestrator
from control_plane.app.modules.identity.api.auth_routes import IdentityHttpRuntime
from control_plane.app.modules.identity.api.super_admin_routes import (
    create_super_admin_router,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from tests.authorization.helpers import authorization_dependencies
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.identity.test_auth_flow import _initialize_account

pytestmark = pytest.mark.integration


def test_super_admin_api_persists_fence_until_post_commit_convergence_and_replay(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_identity_dependencies = identity_dependencies()
    actor_secret, _actor_token = _initialize_account(
        authorization_identity_engine,
        base_identity_dependencies,
        monkeypatch,
        employee_no="00000001",
        display_name="Alice",
    )
    _target_secret, _target_token = _initialize_account(
        authorization_identity_engine,
        base_identity_dependencies,
        monkeypatch,
        employee_no="00000002",
        display_name="Bob",
    )
    with authorization_owner_engine.begin() as db:
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

    authz_dependencies = authorization_dependencies()

    def unavailable_projection(_account_ids: tuple[str, ...]) -> None:
        raise RuntimeError("injected projection outage")

    failing_convergence = SecurityChangeOrchestrator(
        authorization_rw_engine,
        authz_dependencies,
        recompute_membership=unavailable_projection,
    )

    def register_identity_change(account_id: str) -> object:
        source = identity.current_identity_change_source()
        assert source is not None
        return failing_convergence.identity_change(
            account_id,
            actor=source.actor,
            operation=source.operation,
            idempotency_key=source.idempotency_key,
            source_transaction_id=source.source_transaction_id,
            request_fingerprint=source.request_fingerprint,
            idempotency_claim_id=source.idempotency_claim_id,
        )

    dependencies = replace(
        base_identity_dependencies,
        on_auth_change=register_identity_change,
    )
    runtime = IdentityHttpRuntime(
        authorization_identity_engine,
        dependencies,
        failing_convergence,
    )
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
    dependencies.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
    headers = {
        "Idempotency-Key": "task10-convergence-0001",
        "If-Match": f'"v{target.version}"',
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "X-Request-ID": "req-task10fence",
    }
    body = {
        "accountId": str(target.id),
        "totpCode": pyotp.TOTP(actor_secret).at(dependencies.clock.now()),
        "reason": "durable convergence evidence",
    }

    unavailable = client.post(
        "/api/v1/admin/super-admins",
        json=body,
        headers=headers,
    )
    replay_unavailable = client.post(
        "/api/v1/admin/super-admins",
        json=body,
        headers=headers,
    )

    assert unavailable.status_code == 503
    assert unavailable.json()["requestId"] == "req-task10fence"
    assert replay_unavailable.status_code == 503
    with authorization_owner_engine.connect() as db:
        pending = (
            db.execute(
                text(
                    "SELECT w.status, w.source_transaction_id, w.idempotency_claim_id, "
                    "w.request_fingerprint, p.version, p.dirty_generation, a.is_super_admin "
                    'FROM "authorization".convergence_work w '
                    'JOIN "authorization".principal_version p '
                    "ON p.account_id=:account_id "
                    "JOIN identity.account a ON a.id::text=:account_id "
                    "WHERE w.source_module='identity' "
                    "AND w.operation='super_admin_add' "
                    "AND w.idempotency_key='task10-convergence-0001'"
                ),
                {"account_id": str(target.id)},
            )
            .mappings()
            .one()
        )
        claim_id = db.execute(
            text(
                "SELECT id FROM identity.idempotency_record "
                "WHERE operation='super_admin_add' "
                "AND idempotency_key='task10-convergence-0001'"
            )
        ).scalar_one()
    assert pending["status"] == "PENDING"
    assert pending["source_transaction_id"] is not None
    assert str(pending["idempotency_claim_id"]) == str(claim_id)
    assert pending["request_fingerprint"] is not None
    pending_version = int(pending["version"])
    assert pending["dirty_generation"] is not None
    assert pending["is_super_admin"] is True

    recovered_convergence = SecurityChangeOrchestrator(
        authorization_rw_engine,
        authz_dependencies,
        recompute_membership=lambda _account_ids: None,
    )
    assert recovered_convergence.reconcile_pending() is True

    replayed = client.post(
        "/api/v1/admin/super-admins",
        json=body,
        headers=headers,
    )
    assert replayed.status_code == 200
    assert replayed.json()["isSuperAdmin"] is True
    with authorization_owner_engine.connect() as db:
        converged = (
            db.execute(
                text(
                    "SELECT w.status, p.version, p.dirty_generation "
                    'FROM "authorization".convergence_work w '
                    'JOIN "authorization".principal_version p '
                    "ON p.account_id=:account_id "
                    "WHERE w.idempotency_key='task10-convergence-0001'"
                ),
                {"account_id": str(target.id)},
            )
            .mappings()
            .one()
        )
        assert (
            db.execute(
                text(
                    'SELECT count(*) FROM "authorization".convergence_work '
                    "WHERE idempotency_key='task10-convergence-0001'"
                )
            ).scalar_one()
            == 1
        )
    assert converged["status"] == "COMPLETED"
    assert converged["version"] == pending_version + 1
    assert converged["dirty_generation"] is None
