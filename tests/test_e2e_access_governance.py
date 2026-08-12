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
from control_plane.app.modules.identity import validate_session
from control_plane.app.shared.db.settings import DbSettings
from control_plane.tools import bootstrap_admin

pytestmark = pytest.mark.integration

SAME_ORIGIN = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}
SUPER_ADMIN_PASSWORD = "V02Super!Admin#2026"
MEMBER_PASSWORD = "V02Member!Access#2026"


class _FailFirstAuthorizationBegin:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.failed = False

    def begin(self) -> object:
        if not self.failed:
            self.failed = True
            raise OSError("injected authorization provisioning outage")
        return self.engine.begin()


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
    request_ids: dict[str, str],
) -> str:
    login = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": employee_no, "password": temporary_password},
        headers={
            "Idempotency-Key": f"{key}-login",
            "X-Request-ID": request_ids["login"],
        },
    )
    assert login.status_code == 200
    assert login.json() == {"state": "BOOTSTRAP_REQUIRED"}
    password_set = client.post(
        "/api/v1/auth/bootstrap/password",
        json={"password": password},
        headers={
            "Idempotency-Key": f"{key}-password",
            "X-Request-ID": request_ids["password"],
        },
    )
    assert password_set.status_code == 200
    enrollment = client.post(
        "/api/v1/auth/bootstrap/totp/enroll",
        headers={
            "Idempotency-Key": f"{key}-enroll",
            "X-Request-ID": request_ids["enroll"],
        },
    )
    assert enrollment.status_code == 200
    secret = str(parse_qs(urlsplit(enrollment.json()["provisioningUri"]).query)["secret"][0])
    confirmed = client.post(
        "/api/v1/auth/bootstrap/totp/confirm",
        json={"code": pyotp.TOTP(secret).now()},
        headers={
            "Idempotency-Key": f"{key}-confirm",
            "X-Request-ID": request_ids["confirm"],
        },
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
    request_ids: dict[str, str],
) -> None:
    password_step = client.post(
        "/api/v1/auth/login",
        json={"employeeNo": employee_no, "password": password},
        headers={
            "Idempotency-Key": f"{key}-login",
            "X-Request-ID": request_ids["password"],
        },
    )
    assert password_step.status_code == 200
    assert password_step.json()["state"] == "TOTP_REQUIRED"
    totp_step = client.post(
        "/api/v1/auth/totp",
        json={
            "challengeToken": password_step.json()["challengeToken"],
            "code": pyotp.TOTP(secret).at(int(time.time()) + 30),
        },
        headers={
            "Idempotency-Key": f"{key}-totp",
            "X-Request-ID": request_ids["totp"],
        },
    )
    assert totp_step.status_code == 200
    assert totp_step.json() == {"state": "AUTHENTICATED"}


def _audit_actions(owner: Engine) -> dict[str, tuple[str, ...]]:
    with owner.connect() as db:
        rows = (
            db.execute(
                text(
                    "SELECT request_id, action FROM audit.audit_event "
                    "WHERE request_id LIKE 'req-e2e%' ORDER BY request_id, action"
                )
            )
            .tuples()
            .all()
        )
    grouped: dict[str, list[str]] = {}
    for request_id, action in rows:
        grouped.setdefault(str(request_id), []).append(str(action))
    return {request_id: tuple(actions) for request_id, actions in grouped.items()}


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
        authorization_engine=engines["authorization"],
        authorization_dependencies=bootstrap.authorization_dependencies(),
        stdout=stdout,
        stderr=stderr,
    )
    assert exit_code == 0
    temporary_admin_password = stdout.getvalue().strip()
    assert temporary_admin_password
    bootstrap_evidence_lines = [json.loads(line) for line in stderr.getvalue().splitlines()]
    assert [item["result"] for item in bootstrap_evidence_lines] == ["ATTEMPT", "SUCCESS"]
    bootstrap_attempt, bootstrap_evidence = bootstrap_evidence_lines
    assert bootstrap_attempt["commandId"] == bootstrap_evidence["commandId"]
    assert bootstrap_evidence["result"] == "SUCCESS"
    command_id = bootstrap_evidence["commandId"]

    app = bootstrap.create_app(identity_runtime_provider=bootstrap.identity_http_runtime)
    admin = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    admin_secret = _initialize(
        admin,
        employee_no="00000001",
        temporary_password=temporary_admin_password,
        password=SUPER_ADMIN_PASSWORD,
        key="e2e-admin-bootstrap",
        request_ids={
            "login": "req-e2eadmintemp",
            "password": "req-e2eadminpassword",
            "enroll": "req-e2eadminenroll",
            "confirm": "req-e2eadminconfirm",
        },
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

    with engines["authorization"].connect() as db:
        initial_grants = (
            db.execute(
                text(
                    'SELECT capability, scope_type, scope_id, source FROM "authorization"."grant" '
                    "WHERE principal_id=:principal_id ORDER BY capability"
                ),
                {"principal_id": admin_principal.account_id},
            )
            .tuples()
            .all()
        )
    assert initial_grants == [
        ("audit.read", "PLATFORM", None, "SYSTEM_BOOTSTRAP"),
        ("identity.account.manage", "PLATFORM", None, "SYSTEM_BOOTSTRAP"),
        ("platform.authorization.manage", "PLATFORM", None, "SYSTEM_BOOTSTRAP"),
    ]
    with owner.connect() as db:
        bootstrap_audits = (
            db.execute(
                text(
                    "SELECT action, count(*) FROM audit.audit_event "
                    "WHERE correlation_id=:command_id GROUP BY action ORDER BY action"
                ),
                {"command_id": command_id},
            )
            .tuples()
            .all()
        )
        command_facts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM identity.idempotency_record "
                "WHERE operation='super_admin_bootstrap_cli') AS identity_claims, "
                '(SELECT count(*) FROM "authorization".idempotency_record '
                "WHERE operation='initial_admin_provisioning') AS authorization_claims, "
                '(SELECT count(*) FROM "authorization".convergence_work '
                "WHERE source_module='identity' AND operation='super_admin_bootstrap_cli' "
                "AND status='COMPLETED') AS convergence"
            )
        ).one()
    assert bootstrap_audits == [
        ("authorization.grant.created", 3),
        ("authorization.identity.converged", 1),
        ("identity.account.created", 1),
        ("identity.super_admin.bootstrapped", 1),
        ("identity.temp_credential.issued", 1),
    ]
    assert command_facts == (1, 1, 1)

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
        request_ids={
            "login": "req-e2emembertemp",
            "password": "req-e2ememberpassword",
            "enroll": "req-e2ememberenroll",
            "confirm": "req-e2ememberconfirm",
        },
    )
    logged_out = member.post(
        "/api/v1/auth/logout",
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "e2e-member-bootstrap-logout",
            "X-Request-ID": "req-e2ememberlogout",
        },
    )
    assert logged_out.status_code == 200
    _full_login(
        member,
        employee_no="00000002",
        password=MEMBER_PASSWORD,
        secret=member_secret,
        key="e2e-member-full",
        request_ids={
            "password": "req-e2ememberloginpassword",
            "totp": "req-e2ememberlogintotp",
        },
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
    assert (
        member.get(
            "/api/v1/admin/accounts",
            headers={"X-Request-ID": "req-e2eallowed"},
        ).status_code
        == 200
    )

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
        "req-e2eadmintemp": ("identity.temp_credential.consumed",),
        "req-e2eadminpassword": (
            "authorization.identity.converged",
            "identity.password.setup.completed",
        ),
        "req-e2eadminenroll": ("identity.totp.enrolled",),
        "req-e2eadminconfirm": (
            "authorization.identity.converged",
            "identity.sessions.revoked",
            "identity.totp.confirmed",
        ),
        "req-e2ecreate": (
            "identity.account.created",
            "identity.temp_credential.issued",
        ),
        "req-e2emembertemp": ("identity.temp_credential.consumed",),
        "req-e2ememberpassword": (
            "authorization.identity.converged",
            "identity.password.setup.completed",
        ),
        "req-e2ememberenroll": ("identity.totp.enrolled",),
        "req-e2ememberconfirm": (
            "authorization.identity.converged",
            "identity.sessions.revoked",
            "identity.totp.confirmed",
        ),
        "req-e2ememberlogout": (
            "authorization.identity.converged",
            "identity.session.logout",
        ),
        "req-e2ememberloginpassword": ("identity.login.password",),
        "req-e2ememberlogintotp": ("identity.login.totp",),
        "req-e2edeniedbefore": ("authorization.decision",),
        "req-e2egrant": ("authorization.grant.created",),
        "req-e2erevoke": ("authorization.grant.revoked",),
        "req-e2edeniedafter": ("authorization.decision",),
    }
    assert _audit_actions(owner) == expected
    for audit_query_index, (request_id, actions) in enumerate(expected.items(), start=1):
        queried = admin.get(
            "/api/v1/admin/audit-events",
            params={"requestId": request_id},
            headers={"X-Request-ID": f"req-auditverify-{audit_query_index:02d}"},
        )
        assert queried.status_code == 200
        assert sorted(
            (item["requestId"], item["action"]) for item in queried.json()["items"]
        ) == sorted((request_id, action) for action in actions)
    successful_read = admin.get(
        "/api/v1/admin/audit-events",
        params={"requestId": "req-e2eallowed"},
        headers={"X-Request-ID": "req-auditverify-allowed"},
    )
    assert successful_read.status_code == 200
    assert successful_read.json()["items"] == []
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
    with owner.connect() as db:
        persistent_security_text = db.execute(
            text(
                "SELECT concat("
                "coalesce((SELECT string_agg(to_jsonb(e)::text, '') "
                "FROM audit.audit_event e), ''), "
                "coalesce((SELECT string_agg(to_jsonb(a)::text, '') "
                "FROM identity.account a), ''), "
                "coalesce((SELECT string_agg(to_jsonb(t)::text, '') "
                "FROM identity.temp_credential t), ''), "
                "coalesce((SELECT string_agg(to_jsonb(c)::text, '') "
                "FROM identity.auth_challenge c), ''), "
                "coalesce((SELECT string_agg(to_jsonb(s)::text, '') "
                "FROM identity.session s), ''), "
                "coalesce((SELECT string_agg(to_jsonb(i)::text, '') "
                "FROM identity.idempotency_record i), ''), "
                "coalesce((SELECT string_agg(to_jsonb(ai)::text, '') "
                "FROM \"authorization\".idempotency_record ai), ''), "
                "coalesce((SELECT string_agg(to_jsonb(g)::text, '') "
                'FROM "authorization"."grant" g), \'\'), '
                "coalesce((SELECT string_agg(to_jsonb(w)::text, '') "
                "FROM \"authorization\".convergence_work w), ''))"
            )
        ).scalar_one()
    for secret in (
        temporary_admin_password,
        admin_secret,
        temporary_member_password,
        member_secret,
        SUPER_ADMIN_PASSWORD,
        MEMBER_PASSWORD,
    ):
        assert secret not in persistent_security_text
        assert secret not in stderr.getvalue()


def test_bootstrap_cli_recovers_same_command_after_authorization_outage(
    e2e_runtime: tuple[Engine, dict[str, Engine]],
) -> None:
    owner, engines = e2e_runtime
    authorization = _FailFirstAuthorizationBegin(engines["authorization"])
    argv = ["--employee-no", "00000001", "--display-name", "Recoverable Administrator"]

    first_stdout, first_stderr = StringIO(), StringIO()
    first_exit = bootstrap_admin.main(
        argv,
        engine=engines["identity"],
        dependencies=bootstrap.identity_dependencies(),
        security_changes=bootstrap.security_change_orchestrator(),
        authorization_engine=authorization,  # type: ignore[arg-type]
        authorization_dependencies=bootstrap.authorization_dependencies(),
        stdout=first_stdout,
        stderr=first_stderr,
    )
    replay_stdout, replay_stderr = StringIO(), StringIO()
    replay_exit = bootstrap_admin.main(
        argv,
        engine=engines["identity"],
        dependencies=bootstrap.identity_dependencies(),
        security_changes=bootstrap.security_change_orchestrator(),
        authorization_engine=authorization,  # type: ignore[arg-type]
        authorization_dependencies=bootstrap.authorization_dependencies(),
        stdout=replay_stdout,
        stderr=replay_stderr,
    )

    first_evidence_lines = [json.loads(line) for line in first_stderr.getvalue().splitlines()]
    replay_evidence_lines = [json.loads(line) for line in replay_stderr.getvalue().splitlines()]
    assert [item["result"] for item in first_evidence_lines] == ["ATTEMPT", "FAILED"]
    assert [item["result"] for item in replay_evidence_lines] == ["ATTEMPT", "SUCCESS"]
    first_attempt, first_evidence = first_evidence_lines
    replay_attempt, replay_evidence = replay_evidence_lines
    assert first_attempt["commandId"] == first_evidence["commandId"]
    assert replay_attempt["commandId"] == replay_evidence["commandId"]
    assert first_exit == 4
    assert first_evidence["result"] == "FAILED"
    assert replay_exit == 0
    assert replay_evidence["result"] == "SUCCESS"
    assert replay_evidence["commandId"] == first_evidence["commandId"]
    assert replay_stdout.getvalue() == first_stdout.getvalue()
    with owner.connect() as db:
        facts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM identity.account WHERE is_super_admin) AS admins, "
                '(SELECT count(*) FROM "authorization"."grant") AS grants, '
                "(SELECT count(*) FROM audit.audit_event "
                "WHERE action='identity.super_admin.bootstrapped') AS bootstraps, "
                "(SELECT count(*) FROM audit.audit_event "
                "WHERE action='authorization.grant.created') AS grant_audits"
            )
        ).one()
    assert facts == (1, 3, 1, 3)


def test_release_version_is_0_2_0() -> None:
    assert __version__ == "0.2.0"
    assert bootstrap.create_app().openapi()["info"]["version"] == "0.2.0"
