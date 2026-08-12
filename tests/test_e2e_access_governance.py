"""V0.2 access-governance acceptance through the real CLI, HTTP, and facades."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app import __version__
from control_plane.app.modules.authorization import Scope, grant
from control_plane.app.modules.identity import validate_session
from control_plane.app.shared.db.settings import DbSettings
from control_plane.tools import bootstrap_admin

pytestmark = pytest.mark.integration

SAME_ORIGIN = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}
SUPER_ADMIN_PASSWORD = "V02Super!Admin#2026"
MEMBER_PASSWORD = "V02Member!Access#2026"


@contextmanager
def _runtime_engine(
    owner_engine: Engine,
    runtime_url: str,
    *,
    privilege_role: str,
) -> Iterator[Engine]:
    login_role = f"test_e2e_{privilege_role}_{uuid4().hex}"
    quoted_role = f'"{login_role}"'
    password = f"test-only-{uuid4().hex}"
    engine = create_engine(
        make_url(runtime_url).set(username=login_role, password=password),
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "checkout")
    def _set_role(dbapi_connection: object, *_args: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute(f"SET ROLE {privilege_role}")
        finally:
            cursor.close()

    try:
        with owner_engine.begin() as db:
            db.execute(text(f"CREATE ROLE {quoted_role} LOGIN PASSWORD '{password}'"))
            db.execute(text(f"GRANT {privilege_role} TO {quoted_role}"))
        with engine.connect() as db:
            assert db.execute(text("SELECT current_user")).scalar_one() == privilege_role
        yield engine
    finally:
        engine.dispose()
        with owner_engine.begin() as db:
            if db.execute(
                text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname=:role)"),
                {"role": login_role},
            ).scalar_one():
                db.execute(text(f"REVOKE {privilege_role} FROM {quoted_role}"))
                db.execute(text(f"DROP ROLE {quoted_role}"))


@pytest.fixture
def e2e_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Engine, dict[str, Engine]]]:
    settings = DbSettings()
    owner = create_engine(settings.migration_database_url, pool_pre_ping=True)
    try:
        with owner.connect() as db:
            assert db.execute(text("SELECT current_user")).scalar_one() == "platform_owner"
    except Exception:
        owner.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            pytest.fail("Required PostgreSQL integration database unavailable for e2e")
        pytest.skip("PostgreSQL integration database unavailable for e2e")

    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    for name in ("pepper", "totp_key", "idempotency_key"):
        (secret_dir / name).write_bytes(uuid4().bytes + uuid4().bytes)
    monkeypatch.setenv("SECRET_MATERIAL_PATH", str(secret_dir))

    runtime_specs = {
        "audit": (settings.database_url, "audit_rw"),
        "identity": (settings.identity_database_url, "identity_rw"),
        "organization": (settings.organization_database_url, "organization_rw"),
        "workspace": (settings.workspace_database_url, "workspace_rw"),
        "authorization": (settings.authorization_database_url, "authorization_rw"),
        "configuration": (settings.configuration_database_url, "configuration_rw"),
    }
    with ExitStack() as stack:
        engines = {
            name: stack.enter_context(
                _runtime_engine(owner, url, privilege_role=role),
            )
            for name, (url, role) in runtime_specs.items()
        }
        monkeypatch.setattr(bootstrap, "runtime_engine", lambda: engines["audit"])
        monkeypatch.setattr(bootstrap, "identity_runtime_engine", lambda: engines["identity"])
        monkeypatch.setattr(
            bootstrap,
            "organization_runtime_engine",
            lambda: engines["organization"],
        )
        monkeypatch.setattr(bootstrap, "workspace_runtime_engine", lambda: engines["workspace"])
        monkeypatch.setattr(
            bootstrap,
            "authorization_runtime_engine",
            lambda: engines["authorization"],
        )
        monkeypatch.setattr(
            bootstrap,
            "configuration_runtime_engine",
            lambda: engines["configuration"],
        )
        for cached in (
            bootstrap.identity_dependencies,
            bootstrap.organization_dependencies,
            bootstrap.workspace_dependencies,
            bootstrap.authorization_dependencies,
            bootstrap.configuration_dependencies,
            bootstrap.identity_http_runtime,
            bootstrap.authorization_http_runtime,
            bootstrap.organization_http_runtime,
            bootstrap.workspace_http_runtime,
            bootstrap.configuration_http_runtime,
            bootstrap.security_change_orchestrator,
        ):
            cached.cache_clear()
        with owner.begin() as db:
            db.execute(
                text(
                    'TRUNCATE "authorization".convergence_principal_pending, '
                    '"authorization".convergence_work, "authorization".idempotency_record, '
                    '"authorization"."grant", "authorization".principal_version, '
                    "organization.idempotency_record, organization.org_edge, "
                    "workspace.members_projection, workspace.leader, "
                    "workspace.idempotency_record, workspace.workspace, "
                    "identity.idempotency_record, identity.auth_challenge, identity.session, "
                    "identity.temp_credential, identity.login_backoff, identity.account, "
                    "audit.audit_event"
                )
            )
        try:
            yield owner, engines
        finally:
            with owner.begin() as db:
                db.execute(
                    text(
                        'TRUNCATE "authorization".convergence_principal_pending, '
                        '"authorization".convergence_work, "authorization".idempotency_record, '
                        '"authorization"."grant", "authorization".principal_version, '
                        "organization.idempotency_record, organization.org_edge, "
                        "workspace.members_projection, workspace.leader, "
                        "workspace.idempotency_record, workspace.workspace, "
                        "identity.idempotency_record, identity.auth_challenge, identity.session, "
                        "identity.temp_credential, identity.login_backoff, identity.account, "
                        "audit.audit_event"
                    )
                )
            for cached in (
                bootstrap.identity_dependencies,
                bootstrap.organization_dependencies,
                bootstrap.workspace_dependencies,
                bootstrap.authorization_dependencies,
                bootstrap.configuration_dependencies,
                bootstrap.identity_http_runtime,
                bootstrap.authorization_http_runtime,
                bootstrap.organization_http_runtime,
                bootstrap.workspace_http_runtime,
                bootstrap.configuration_http_runtime,
                bootstrap.security_change_orchestrator,
            ):
                cached.cache_clear()
    owner.dispose()


def _initialize(
    client: TestClient,
    *,
    employee_no: str,
    temporary_password: str,
    password: str,
    key: str,
) -> str:
    login = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": employee_no, "password": temporary_password},
        headers={"Idempotency-Key": f"{key}-login"},
    )
    assert login.status_code == 200
    assert login.json() == {"state": "BOOTSTRAP_REQUIRED"}
    password_set = client.post(
        "/api/v1/auth/bootstrap/password",
        json={"password": password},
        headers={"Idempotency-Key": f"{key}-password"},
    )
    assert password_set.status_code == 200
    enrollment = client.post(
        "/api/v1/auth/bootstrap/totp/enroll",
        headers={"Idempotency-Key": f"{key}-enroll"},
    )
    assert enrollment.status_code == 200
    secret = str(parse_qs(urlsplit(enrollment.json()["provisioningUri"]).query)["secret"][0])
    confirmed = client.post(
        "/api/v1/auth/bootstrap/totp/confirm",
        json={"code": pyotp.TOTP(secret).now()},
        headers={"Idempotency-Key": f"{key}-confirm"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {"state": "AUTHENTICATED"}
    return secret


def _full_login(
    client: TestClient,
    *,
    employee_no: str,
    password: str,
    secret: str,
    key: str,
) -> None:
    password_step = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": employee_no, "password": password},
        headers={"Idempotency-Key": f"{key}-login"},
    )
    assert password_step.status_code == 200
    assert password_step.json()["state"] == "TOTP_REQUIRED"
    totp_step = client.post(
        "/api/v1/auth/totp",
        json={
            "challengeToken": password_step.json()["challengeToken"],
            "code": pyotp.TOTP(secret).at(int(time.time()) + 30),
        },
        headers={"Idempotency-Key": f"{key}-totp"},
    )
    assert totp_step.status_code == 200
    assert totp_step.json() == {"state": "AUTHENTICATED"}


def test_access_governance_closes_the_real_cli_http_grant_and_audit_loop(
    e2e_runtime: tuple[Engine, dict[str, Engine]],
) -> None:
    owner, engines = e2e_runtime
    stdout, stderr = StringIO(), StringIO()
    exit_code = bootstrap_admin.main(
        ["--employee-no", "00000001", "--display-name", "V0.2 Administrator"],
        engine=engines["identity"],
        dependencies=bootstrap.identity_dependencies(),
        security_changes=bootstrap.security_change_orchestrator(),
        stdout=stdout,
        stderr=stderr,
    )
    assert exit_code == 0
    temporary_admin_password = stdout.getvalue().strip()
    assert temporary_admin_password
    assert json.loads(stderr.getvalue())["result"] == "SUCCESS"

    app = bootstrap.create_app(identity_runtime_provider=bootstrap.identity_http_runtime)
    admin = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    admin_secret = _initialize(
        admin,
        employee_no="00000001",
        temporary_password=temporary_admin_password,
        password=SUPER_ADMIN_PASSWORD,
        key="e2e-admin-bootstrap",
    )
    admin_token = admin.cookies.get("ep_session")
    assert admin_token
    with engines["identity"].begin() as db:
        admin_principal = validate_session(
            db,
            raw_token=admin_token,
            dependencies=bootstrap.identity_dependencies(),
            touch_activity=False,
        )
    assert admin_principal is not None and admin_principal.is_super_admin

    # A Super Admin is not a universal role. Bootstrap provisioning explicitly grants
    # this authenticated principal the ordinary capabilities needed by this scenario.
    with engines["authorization"].begin() as db:
        for capability in (
            "identity.account.manage",
            "platform.authorization.manage",
            "audit.read",
        ):
            grant(
                db,
                principal_id=admin_principal.account_id,
                capability=capability,
                scope=Scope.platform(),
                actor=admin_principal,
                reason="V0.2 end-to-end acceptance bootstrap",
                dependencies=bootstrap.authorization_dependencies(),
                source="SYSTEM_BOOTSTRAP",
            )

    created = admin.post(
        "/api/v1/admin/accounts",
        json={
            "employeeNo": "00000002",
            "displayName": "V0.2 Member",
            "profession": "BACKEND",
            "reason": "V0.2 end-to-end acceptance",
        },
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "e2e-create-member",
            "X-Request-ID": "req-e2ecreate",
        },
    )
    assert created.status_code == 201
    member_id = created.json()["account"]["id"]
    temporary_member_password = created.json()["temporaryPassword"]

    member = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    member_secret = _initialize(
        member,
        employee_no="00000002",
        temporary_password=temporary_member_password,
        password=MEMBER_PASSWORD,
        key="e2e-member-bootstrap",
    )
    logged_out = member.post(
        "/api/v1/auth/logout",
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "e2e-member-bootstrap-logout",
        },
    )
    assert logged_out.status_code == 200
    _full_login(
        member,
        employee_no="00000002",
        password=MEMBER_PASSWORD,
        secret=member_secret,
        key="e2e-member-full",
    )

    denied_before = member.get(
        "/api/v1/admin/accounts",
        headers={"X-Request-ID": "req-e2edeniedbefore"},
    )
    assert denied_before.status_code == 403
    assert denied_before.json()["requestId"] == "req-e2edeniedbefore"

    granted = admin.post(
        "/api/v1/admin/grants",
        json={
            "principalId": member_id,
            "capability": "identity.account.manage",
            "scopeType": "PLATFORM",
            "source": "MANUAL",
            "reason": "V0.2 end-to-end exact grant",
        },
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "e2e-grant-member",
            "X-Request-ID": "req-e2egrant",
        },
    )
    assert granted.status_code == 201
    grant_id = granted.json()["id"]
    assert member.get("/api/v1/admin/accounts").status_code == 200

    revoked = admin.request(
        "DELETE",
        f"/api/v1/admin/grants/{grant_id}",
        json={"reason": "V0.2 end-to-end immediate revoke"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "e2e-revoke-member",
            "If-Match": granted.headers["etag"],
            "X-Request-ID": "req-e2erevoke",
        },
    )
    assert revoked.status_code == 200
    denied_after = member.get(
        "/api/v1/admin/accounts",
        headers={"X-Request-ID": "req-e2edeniedafter"},
    )
    assert denied_after.status_code == 403
    assert denied_after.json()["requestId"] == "req-e2edeniedafter"

    expected = {
        "req-e2ecreate": (
            "identity.account.created",
            "identity.temp_credential.issued",
        ),
        "req-e2edeniedbefore": ("authorization.decision",),
        "req-e2egrant": ("authorization.grant.created",),
        "req-e2erevoke": ("authorization.grant.revoked",),
        "req-e2edeniedafter": ("authorization.decision",),
    }
    for request_id, actions in expected.items():
        queried = admin.get(
            "/api/v1/admin/audit-events",
            params={"requestId": request_id},
        )
        assert queried.status_code == 200
        assert sorted(
            (item["requestId"], item["action"]) for item in queried.json()["items"]
        ) == sorted((request_id, action) for action in actions)
    with owner.connect() as db:
        critical_rows = db.execute(
            text(
                "SELECT request_id, action, count(*) FROM audit.audit_event "
                "WHERE request_id = ANY(:request_ids) GROUP BY request_id, action"
            ),
            {"request_ids": list(expected)},
        ).all()
    assert {(str(row[0]), str(row[1]), int(row[2])) for row in critical_rows} == {
        (request_id, action, 1) for request_id, actions in expected.items() for action in actions
    }
    database_text = ""
    with owner.connect() as db:
        database_text = db.execute(
            text("SELECT coalesce(string_agg(to_jsonb(e)::text, ''), '') FROM audit.audit_event e")
        ).scalar_one()
    for secret in (
        temporary_admin_password,
        admin_secret,
        temporary_member_password,
        member_secret,
        SUPER_ADMIN_PASSWORD,
        MEMBER_PASSWORD,
    ):
        assert secret not in database_text


def test_release_version_is_0_2_0() -> None:
    assert __version__ == "0.2.0"
    assert bootstrap.create_app().openapi()["info"]["version"] == "0.2.0"
