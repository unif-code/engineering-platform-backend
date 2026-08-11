from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import control_plane.app.bootstrap.app as bootstrap
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
from tests.identity.test_auth_flow import _initialize_account
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
