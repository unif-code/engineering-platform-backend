from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import control_plane.app.bootstrap.app as bootstrap
import control_plane.app.modules.identity.api.auth_routes as identity_auth_routes
import control_plane.app.modules.organization as organization
from control_plane.app.modules.authorization import (
    DecisionDependencies,
    Scope,
    SecurityChangeOrchestrator,
    grant,
)
from control_plane.app.modules.authorization.adapters import (
    SqlAlchemyIdentitySessionValidator,
    SqlAlchemyOrganizationSummary,
    SqlAlchemyWorkspaceMembership,
    SqlAlchemyWorkspaceSummaries,
)
from control_plane.app.modules.authorization.api import AuthorizationHttpRuntime
from control_plane.app.modules.identity import Principal, SessionKind, SessionPrincipal
from control_plane.app.modules.identity.api.auth_routes import IdentityHttpRuntime
from control_plane.app.modules.organization.api import OrganizationHttpRuntime
from control_plane.app.modules.workspace import on_membership_change
from control_plane.app.modules.workspace.api import WorkspaceHttpRuntime
from control_plane.app.shared.db.settings import DbSettings
from tests.authorization.helpers import authorization_dependencies
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.identity.test_auth_flow import VALID_PASSWORD, _initialize_account
from tests.organization.conftest import _temporary_organization_role_engine
from tests.organization.helpers import insert_account, organization_dependencies
from tests.workspace.conftest import _temporary_workspace_role_engine
from tests.workspace.helpers import workspace_dependencies

pytestmark = pytest.mark.integration


@pytest.fixture
def replay_workspace_engine(
    authorization_owner_engine: Engine,
) -> Iterator[Engine]:
    with _temporary_workspace_role_engine(
        authorization_owner_engine,
        DbSettings().workspace_database_url,
    ) as runtime:
        yield runtime[0]


@pytest.fixture
def replay_organization_engine(
    authorization_owner_engine: Engine,
) -> Iterator[Engine]:
    with _temporary_organization_role_engine(
        authorization_owner_engine,
        DbSettings().organization_database_url,
    ) as runtime:
        yield runtime[0]


@pytest.fixture
def clean_replay_workspace_db(
    authorization_owner_engine: Engine,
) -> Iterator[None]:
    tables = (
        "workspace.members_projection, workspace.leader, workspace.idempotency_record, "
        "workspace.workspace, organization.idempotency_record, organization.org_edge"
    )
    with authorization_owner_engine.begin() as db:
        db.execute(text(f"TRUNCATE {tables}"))
    yield
    with authorization_owner_engine.begin() as db:
        db.execute(text(f"TRUNCATE {tables}"))


def test_root_wiring_completed_workspace_replay_has_no_new_fence_or_projection_audit(
    clean_authorization_db: None,
    clean_replay_workspace_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    replay_workspace_engine: Engine,
    replay_organization_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_deps = identity_dependencies()
    _secret, token = _initialize_account(
        authorization_identity_engine,
        identity_deps,
        monkeypatch,
    )
    with authorization_owner_engine.connect() as db:
        owner_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())
    manager_id = "00000000-0000-0000-0000-000000009991"
    insert_account(
        authorization_owner_engine,
        account_id=manager_id,
        employee_no="00009991",
        display_name="Manager",
    )
    organization_deps = organization_dependencies(
        authorization_identity_engine,
        on_membership_change=lambda _ids: None,
    )
    system = Principal(employee_id="SYSTEM", name="System")
    with replay_organization_engine.begin() as db:
        organization.set_superior(
            db,
            account_id=manager_id,
            superior_id=None,
            actor=system,
            reason="root replay manager",
            dependencies=organization_deps,
        )
        organization.set_superior(
            db,
            account_id=owner_id,
            superior_id=manager_id,
            actor=system,
            reason="root replay leader",
            dependencies=organization_deps,
        )

    workspace_deps = workspace_dependencies(
        authorization_identity_engine,
        replay_organization_engine,
    )
    authz_deps = authorization_dependencies()
    security_changes = SecurityChangeOrchestrator(
        authorization_rw_engine,
        authz_deps,
        recompute_membership=on_membership_change(
            replay_workspace_engine,
            dependencies=workspace_deps,
        ),
    )
    membership = SqlAlchemyWorkspaceMembership(replay_workspace_engine, workspace_deps)
    authorization_runtime = AuthorizationHttpRuntime(
        engine=authorization_rw_engine,
        dependencies=authz_deps,
        decision_dependencies=DecisionDependencies(
            identity=SqlAlchemyIdentitySessionValidator(
                authorization_identity_engine,
                identity_deps,
            ),
            workspace=membership,
            reconcile=security_changes.reconcile_for_account,
        ),
        organization_summary=SqlAlchemyOrganizationSummary(
            replay_organization_engine,
            organization_deps,
        ),
        workspace_summaries=SqlAlchemyWorkspaceSummaries(
            replay_workspace_engine,
            workspace_deps,
        ),
    )
    organization_runtime = OrganizationHttpRuntime(
        engine=replay_organization_engine,
        dependencies=organization_deps,
        security_changes=security_changes,
    )
    workspace_runtime = WorkspaceHttpRuntime(
        engine=replay_workspace_engine,
        dependencies=workspace_deps,
        security_changes=security_changes,
    )
    with authorization_rw_engine.begin() as db:
        grant(
            db,
            principal_id=owner_id,
            capability="platform.workspace.manage",
            scope=Scope.platform(),
            actor=SessionPrincipal(
                account_id=owner_id,
                employee_no="00000001",
                display_name="Alice",
                session_kind=SessionKind.FULL,
                is_super_admin=False,
            ),
            reason="create workspace in root replay test",
            dependencies=authz_deps,
        )

    monkeypatch.setattr(bootstrap, "authorization_http_runtime", lambda: authorization_runtime)
    monkeypatch.setattr(bootstrap, "organization_http_runtime", lambda: organization_runtime)
    monkeypatch.setattr(bootstrap, "workspace_http_runtime", lambda: workspace_runtime)
    app = bootstrap.create_app(
        identity_runtime_provider=lambda: IdentityHttpRuntime(
            engine=authorization_identity_engine,
            dependencies=identity_deps,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    client.cookies.set("ep_session", token)
    headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "Idempotency-Key": "workspace-root-replay-001",
    }
    body = {"name": "Replay Safe", "ownerId": owner_id, "reason": "create"}

    created = client.post("/api/v1/admin/workspaces", headers=headers, json=body)
    assert created.status_code == 201
    with authorization_owner_engine.connect() as db:
        before = db.execute(
            text(
                "SELECT pv.version, pv.fence_generation, pv.dirty_generation, "
                "(SELECT count(*) FROM audit.audit_event "
                " WHERE action='workspace.members.recomputed'), "
                '(SELECT count(*) FROM "authorization".convergence_work '
                " WHERE idempotency_key='workspace-root-replay-001') "
                'FROM "authorization".principal_version pv '
                "WHERE pv.account_id=:account_id"
            ),
            {"account_id": owner_id},
        ).one()

    replay = client.post("/api/v1/admin/workspaces", headers=headers, json=body)
    assert replay.status_code == 201
    assert replay.json() == created.json()
    assert replay.headers["etag"] == created.headers["etag"]
    with authorization_owner_engine.connect() as db:
        after = db.execute(
            text(
                "SELECT pv.version, pv.fence_generation, pv.dirty_generation, "
                "(SELECT count(*) FROM audit.audit_event "
                " WHERE action='workspace.members.recomputed'), "
                '(SELECT count(*) FROM "authorization".convergence_work '
                " WHERE idempotency_key='workspace-root-replay-001') "
                'FROM "authorization".principal_version pv '
                "WHERE pv.account_id=:account_id"
            ),
            {"account_id": owner_id},
        ).one()
    assert after == before
    assert after.dirty_generation is None
    assert after[3:] == (1, 1)


def test_root_wiring_identity_logout_converges_only_after_source_commit_and_replays_once(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dependencies = identity_dependencies()
    _secret, token = _initialize_account(
        authorization_identity_engine,
        setup_dependencies,
        monkeypatch,
    )
    with authorization_owner_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())

    recomputes: list[tuple[str, ...]] = []
    orchestrator = SecurityChangeOrchestrator(
        authorization_rw_engine,
        authorization_dependencies(),
        recompute_membership=lambda account_ids: recomputes.append(account_ids),
    )
    monkeypatch.setattr(bootstrap, "security_change_orchestrator", lambda: orchestrator)
    runtime = IdentityHttpRuntime(
        engine=authorization_identity_engine,
        dependencies=replace(
            setup_dependencies,
            on_auth_change=bootstrap._identity_authorization_change,
        ),
        security_changes=orchestrator,
    )
    client = TestClient(
        bootstrap.create_app(identity_runtime_provider=lambda: runtime),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    client.cookies.set("ep_session", token)
    headers = {"Idempotency-Key": "identity-root-logout-001"}

    first = client.post("/api/v1/auth/logout", headers=headers)
    client.cookies.set("ep_session", token)
    replay = client.post("/api/v1/auth/logout", headers=headers)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    with authorization_owner_engine.connect() as db:
        work = db.execute(
            text(
                "SELECT status, source_transaction_id, idempotency_claim_id "
                'FROM "authorization".convergence_work '
                "WHERE source_module='identity' AND operation='auth_logout'"
            )
        ).one()
        version = db.execute(
            text(
                'SELECT version, dirty_generation FROM "authorization".principal_version '
                "WHERE account_id=:account_id"
            ),
            {"account_id": account_id},
        ).one()
    assert work.status == "COMPLETED"
    assert work.source_transaction_id is not None
    assert work.idempotency_claim_id is not None
    assert version == (2, None)
    assert recomputes == [(account_id,)]


def test_identity_validator_converges_idle_revocation_without_a_valid_principal(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dependencies = identity_dependencies()
    _secret, token = _initialize_account(
        authorization_identity_engine,
        setup_dependencies,
        monkeypatch,
    )
    setup_dependencies.clock.value += timedelta(minutes=61)  # type: ignore[attr-defined]
    with authorization_owner_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())

    recomputes: list[tuple[str, ...]] = []
    orchestrator = SecurityChangeOrchestrator(
        authorization_rw_engine,
        authorization_dependencies(),
        recompute_membership=lambda account_ids: recomputes.append(account_ids),
    )
    monkeypatch.setattr(bootstrap, "security_change_orchestrator", lambda: orchestrator)
    dependencies = replace(
        setup_dependencies,
        on_auth_change=bootstrap._identity_authorization_change,
    )
    validator = SqlAlchemyIdentitySessionValidator(
        authorization_identity_engine,
        dependencies,
        security_changes=orchestrator,
    )

    assert validator.validate(token) is None

    with authorization_owner_engine.connect() as db:
        session = db.execute(
            text("SELECT revoked_at, revoke_reason FROM identity.session WHERE kind='FULL'")
        ).one()
        work = db.execute(
            text(
                'SELECT status, source_transaction_id FROM "authorization".convergence_work '
                "WHERE source_module='identity' AND operation='identity_session_validate'"
            )
        ).one()
        version = db.execute(
            text(
                'SELECT version, dirty_generation FROM "authorization".principal_version '
                "WHERE account_id=:account_id"
            ),
            {"account_id": account_id},
        ).one()
    assert session.revoked_at is not None
    assert session.revoke_reason == "IDLE_TIMEOUT"
    assert work.status == "COMPLETED"
    assert work.source_transaction_id is not None
    assert version == (2, None)
    assert recomputes == [(account_id,)]


def test_identity_projection_failure_returns_503_then_fresh_reconciler_recovers_replay(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dependencies = identity_dependencies()
    _secret, token = _initialize_account(
        authorization_identity_engine,
        setup_dependencies,
        monkeypatch,
    )
    with authorization_owner_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())

    attempts: list[tuple[str, ...]] = []

    def unavailable(account_ids: tuple[str, ...]) -> None:
        attempts.append(account_ids)
        raise RuntimeError("projection unavailable")

    authorization_deps = authorization_dependencies()
    failing = SecurityChangeOrchestrator(
        authorization_rw_engine,
        authorization_deps,
        recompute_membership=unavailable,
    )
    monkeypatch.setattr(bootstrap, "security_change_orchestrator", lambda: failing)
    runtime = IdentityHttpRuntime(
        engine=authorization_identity_engine,
        dependencies=replace(
            setup_dependencies,
            on_auth_change=bootstrap._identity_authorization_change,
        ),
        security_changes=failing,
    )
    client = TestClient(
        bootstrap.create_app(identity_runtime_provider=lambda: runtime),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    client.cookies.set("ep_session", token)
    headers = {"Idempotency-Key": "identity-projection-recovery-001"}

    first = client.post("/api/v1/auth/logout", headers=headers)
    client.cookies.set("ep_session", token)
    unavailable_replay = client.post("/api/v1/auth/logout", headers=headers)

    assert first.status_code == unavailable_replay.status_code == 503
    with authorization_owner_engine.connect() as db:
        pending = db.execute(
            text(
                'SELECT status FROM "authorization".convergence_work '
                "WHERE idempotency_key='identity-projection-recovery-001'"
            )
        ).scalar_one()
    assert pending == "PENDING"

    recovered = SecurityChangeOrchestrator(
        authorization_rw_engine,
        replace(authorization_deps),
        recompute_membership=lambda account_ids: attempts.append(account_ids),
    )
    assert recovered.reconcile_pending() is True
    client.cookies.set("ep_session", token)
    replay = client.post("/api/v1/auth/logout", headers=headers)

    assert replay.status_code == 200
    with authorization_owner_engine.connect() as db:
        counts = db.execute(
            text(
                'SELECT (SELECT count(*) FROM "authorization".convergence_work '
                " WHERE idempotency_key='identity-projection-recovery-001'), "
                "(SELECT count(*) FROM identity.idempotency_record "
                " WHERE idempotency_key='identity-projection-recovery-001'), "
                '(SELECT version FROM "authorization".principal_version '
                " WHERE account_id=:account_id), "
                '(SELECT dirty_generation FROM "authorization".principal_version '
                " WHERE account_id=:account_id)"
            ),
            {"account_id": account_id},
        ).one()
        status = db.execute(
            text(
                'SELECT status FROM "authorization".convergence_work '
                "WHERE idempotency_key='identity-projection-recovery-001'"
            )
        ).scalar_one()
    assert counts == (1, 1, 2, None)
    assert status == "COMPLETED"
    assert attempts == [(account_id,), (account_id,)]


def test_identity_source_rollback_cancels_registered_work_without_version_bump(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dependencies = identity_dependencies()
    _secret, token = _initialize_account(
        authorization_identity_engine,
        setup_dependencies,
        monkeypatch,
    )
    with authorization_owner_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())

    recomputes: list[tuple[str, ...]] = []
    orchestrator = SecurityChangeOrchestrator(
        authorization_rw_engine,
        authorization_dependencies(),
        recompute_membership=lambda account_ids: recomputes.append(account_ids),
    )
    monkeypatch.setattr(bootstrap, "security_change_orchestrator", lambda: orchestrator)
    original_logout = cast(Any, identity_auth_routes).logout

    def rollback_after_registration(*args: object, **kwargs: object) -> bool:
        original_logout(*args, **kwargs)
        raise RuntimeError("source transaction failure after security registration")

    monkeypatch.setattr(identity_auth_routes, "logout", rollback_after_registration)
    runtime = IdentityHttpRuntime(
        engine=authorization_identity_engine,
        dependencies=replace(
            setup_dependencies,
            on_auth_change=bootstrap._identity_authorization_change,
        ),
        security_changes=orchestrator,
    )
    client = TestClient(
        bootstrap.create_app(identity_runtime_provider=lambda: runtime),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    client.cookies.set("ep_session", token)

    response = client.post(
        "/api/v1/auth/logout",
        headers={"Idempotency-Key": "identity-rollback-001"},
    )

    assert response.status_code == 500
    with authorization_owner_engine.connect() as db:
        session = db.execute(
            text("SELECT revoked_at, revoke_reason FROM identity.session WHERE kind='FULL'")
        ).one()
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM identity.idempotency_record "
                " WHERE idempotency_key='identity-rollback-001'), "
                '(SELECT version FROM "authorization".principal_version '
                " WHERE account_id=:account_id), "
                '(SELECT dirty_generation FROM "authorization".principal_version '
                " WHERE account_id=:account_id)"
            ),
            {"account_id": account_id},
        ).one()
        work = db.execute(
            text(
                'SELECT status, source_transaction_id FROM "authorization".convergence_work '
                "WHERE idempotency_key='identity-rollback-001'"
            )
        ).one()
    assert session == (None, None)
    assert counts == (0, 1, None)
    assert work.status == "CANCELLED"
    assert work.source_transaction_id is not None
    assert recomputes == []


def test_identity_success_replay_without_security_change_needs_no_convergence_work(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_dependencies = identity_dependencies()
    _initialize_account(
        authorization_identity_engine,
        setup_dependencies,
        monkeypatch,
    )
    orchestrator = SecurityChangeOrchestrator(
        authorization_rw_engine,
        authorization_dependencies(),
        recompute_membership=lambda _account_ids: None,
    )
    monkeypatch.setattr(bootstrap, "security_change_orchestrator", lambda: orchestrator)
    runtime = IdentityHttpRuntime(
        engine=authorization_identity_engine,
        dependencies=replace(
            setup_dependencies,
            on_auth_change=bootstrap._identity_authorization_change,
        ),
        security_changes=orchestrator,
    )
    client = TestClient(
        bootstrap.create_app(identity_runtime_provider=lambda: runtime),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    headers = {"Idempotency-Key": "identity-no-security-change-001"}
    body = {"employeeNo": "00000001", "password": VALID_PASSWORD}

    first = client.post("/api/v1/auth/login", headers=headers, json=body)
    replay = client.post("/api/v1/auth/login", headers=headers, json=body)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    with authorization_rw_engine.connect() as db:
        assert (
            db.execute(
                text(
                    'SELECT count(*) FROM "authorization".convergence_work '
                    "WHERE idempotency_key='identity-no-security-change-001'"
                )
            ).scalar_one()
            == 0
        )
