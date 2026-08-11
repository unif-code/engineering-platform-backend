from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.authorization import (
    DecisionCode,
    DecisionDependencies,
    Scope,
    authorize,
    grant,
    mark_fence,
)
from control_plane.app.modules.authorization.adapters import (
    SqlAlchemyIdentitySessionValidator,
)
from control_plane.app.modules.identity import SessionKind, SessionPrincipal
from tests.authorization.helpers import authorization_dependencies
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.identity.test_auth_flow import _initialize_account

pytestmark = pytest.mark.integration


@dataclass
class Membership:
    result: bool = True
    error: Exception | None = None
    calls: list[tuple[str, str]] | None = None

    def is_formal_member(self, workspace_id: str, account_id: str) -> bool:
        if self.calls is not None:
            self.calls.append((workspace_id, account_id))
        if self.error is not None:
            raise self.error
        return self.result


def _decision_dependencies(identity_engine: Engine, membership: Membership) -> DecisionDependencies:
    return DecisionDependencies(
        identity=SqlAlchemyIdentitySessionValidator(
            identity_engine,
            identity_dependencies(),
        ),
        workspace=membership,
    )


def _insert_initial_version(authorization_rw_engine: Engine, account_id: str) -> None:
    with authorization_rw_engine.begin() as db:
        db.execute(
            text(
                'INSERT INTO "authorization".principal_version '
                "(account_id, version, fence_generation, updated_at) "
                "VALUES (:account_id, 1, 0, now())"
            ),
            {"account_id": account_id},
        )


def test_real_full_session_and_exact_platform_grant_permit_without_touching_activity(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_deps = identity_dependencies()
    _secret, token = _initialize_account(
        authorization_identity_engine,
        identity_deps,
        monkeypatch,
    )
    with authorization_owner_engine.connect() as db:
        account_id, before = db.execute(
            text(
                "SELECT a.id, s.last_seen_at FROM identity.account a "
                "JOIN identity.session s ON s.account_id=a.id WHERE s.kind='FULL'"
            )
        ).one()
    authz_deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        grant(
            db,
            principal_id=str(account_id),
            capability="platform.organization.read",
            scope=Scope.platform(),
            actor=SessionPrincipal(
                account_id=str(account_id),
                employee_no="00000001",
                display_name="Alice",
                session_kind=SessionKind.FULL,
                is_super_admin=False,
            ),
            reason="permit read",
            dependencies=authz_deps,
        )
    with authorization_rw_engine.begin() as db:
        decision = authorize(
            db,
            raw_token=token,
            capability="platform.organization.read",
            scope=Scope.platform(),
            dependencies=authz_deps,
            decision_dependencies=_decision_dependencies(
                authorization_identity_engine,
                Membership(),
            ),
        )
    assert decision.allowed is True
    assert decision.principal is not None
    assert decision.principal.account_id == str(account_id)
    with authorization_owner_engine.connect() as db:
        after = db.execute(
            text("SELECT last_seen_at FROM identity.session WHERE kind='FULL'")
        ).scalar_one()
    assert after == before


def test_denial_and_dirty_fence_persist_safe_audit(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_deps = identity_dependencies()
    _secret, token = _initialize_account(
        authorization_identity_engine,
        identity_deps,
        monkeypatch,
    )
    with authorization_owner_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())
    _insert_initial_version(authorization_rw_engine, account_id)
    authz_deps = authorization_dependencies()
    decision_deps = _decision_dependencies(authorization_identity_engine, Membership())

    with authorization_rw_engine.begin() as db:
        denied = authorize(
            db,
            raw_token=token,
            capability="platform.organization.read",
            scope=Scope.platform(),
            dependencies=authz_deps,
            decision_dependencies=decision_deps,
        )
    assert denied.code is DecisionCode.DENIED

    with authorization_rw_engine.begin() as db:
        mark_fence(
            db,
            account_ids=[account_id],
            reason="organization convergence",
            dependencies=authz_deps,
        )
    with authorization_rw_engine.begin() as db:
        unavailable = authorize(
            db,
            raw_token=token,
            capability="platform.organization.read",
            scope=Scope.platform(),
            dependencies=authz_deps,
            decision_dependencies=decision_deps,
        )
    assert unavailable.code is DecisionCode.UNAVAILABLE
    with authorization_owner_engine.connect() as db:
        rows = db.execute(
            text(
                "SELECT result, reason FROM audit.audit_event "
                "WHERE action='authorization.decision' ORDER BY occurred_at, id"
            )
        ).all()
    assert sorted(row[0] for row in rows) == ["DENY", "UNAVAILABLE"]
    assert all(token not in (row[1] or "") for row in rows)


def test_workspace_scope_requires_exact_grant_and_current_membership(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_deps = identity_dependencies()
    _secret, token = _initialize_account(
        authorization_identity_engine,
        identity_deps,
        monkeypatch,
    )
    with authorization_owner_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())
    authz_deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        grant(
            db,
            principal_id=account_id,
            capability="platform.workspace.read",
            scope=Scope.workspace("workspace-1"),
            actor=SessionPrincipal(
                account_id=account_id,
                employee_no="00000001",
                display_name="Alice",
                session_kind=SessionKind.FULL,
                is_super_admin=False,
            ),
            reason="workspace one only",
            dependencies=authz_deps,
        )
    calls: list[tuple[str, str]] = []
    with authorization_rw_engine.begin() as db:
        wrong_scope = authorize(
            db,
            raw_token=token,
            capability="platform.workspace.read",
            scope=Scope.workspace("workspace-2"),
            dependencies=authz_deps,
            decision_dependencies=_decision_dependencies(
                authorization_identity_engine,
                Membership(calls=calls),
            ),
        )
    assert wrong_scope.code is DecisionCode.DENIED
    assert calls == []

    with authorization_rw_engine.begin() as db:
        non_member = authorize(
            db,
            raw_token=token,
            capability="platform.workspace.read",
            scope=Scope.workspace("workspace-1"),
            dependencies=authz_deps,
            decision_dependencies=_decision_dependencies(
                authorization_identity_engine,
                Membership(result=False),
            ),
        )
    assert non_member.code is DecisionCode.DENIED

    with authorization_rw_engine.begin() as db:
        projection_failure = authorize(
            db,
            raw_token=token,
            capability="platform.workspace.read",
            scope=Scope.workspace("workspace-1"),
            dependencies=authz_deps,
            decision_dependencies=_decision_dependencies(
                authorization_identity_engine,
                Membership(error=RuntimeError("database details")),
            ),
        )
    assert projection_failure.code is DecisionCode.UNAVAILABLE
