from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.authorization import (
    V02_SUPER_ADMIN_PLATFORM_CAPABILITIES,
    AuthorizationPrincipal,
    AuthorizationUnavailable,
    DecisionCode,
    DecisionDependencies,
    Scope,
    authorize,
    grant,
    principal_has_capability,
    resolve_principal,
)
from control_plane.app.modules.authorization.adapters import (
    SqlAlchemyAuthorizationRepository,
    SqlAlchemyIdentitySessionValidator,
)
from control_plane.app.modules.identity import SessionKind, SessionPrincipal
from tests.authorization.helpers import authorization_dependencies
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.identity.test_auth_flow import _initialize_account

pytestmark = pytest.mark.integration

EXPECTED_V02_SUPER_ADMIN_CAPABILITIES = {
    "platform.home.read",
    "platform.admin.access",
    "audit.read",
    "identity.account.manage",
    "platform.organization.manage",
    "platform.workspace.manage",
    "platform.authorization.manage",
    "platform.configuration.manage",
    "platform.super_admin.manage",
}


@dataclass
class _Membership:
    def is_formal_member(self, workspace_id: str, account_id: str) -> bool:
        return True


def _decision_dependencies(identity_engine: Engine) -> DecisionDependencies:
    return DecisionDependencies(
        identity=SqlAlchemyIdentitySessionValidator(
            identity_engine,
            identity_dependencies(),
        ),
        workspace=_Membership(),
    )


def _initialize_current_super_admin(
    *,
    identity_engine: Engine,
    authorization_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    _secret, token = _initialize_account(
        identity_engine,
        identity_dependencies(),
        monkeypatch,
    )
    with owner_engine.begin() as db:
        account_id = str(
            db.execute(
                text(
                    "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                    "RETURNING id"
                )
            ).scalar_one()
        )
    with authorization_engine.begin() as db:
        db.execute(
            text(
                'INSERT INTO "authorization".principal_version '
                "(account_id, version, fence_generation, updated_at) "
                "VALUES (:account_id, 1, 0, now())"
            ),
            {"account_id": account_id},
        )
    return account_id, token


@pytest.mark.parametrize(
    "reserved_capability",
    [
        "platform.configuration.manage",
        "platform.super_admin.manage",
    ],
)
def test_reserved_capability_ignores_ordinary_grant_in_decision_and_resource_guard(
    reserved_capability: str,
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
    actor = SessionPrincipal(
        account_id=account_id,
        employee_no="00000001",
        display_name="Alice",
        session_kind=SessionKind.FULL,
        is_super_admin=False,
    )
    dependencies = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        grant(
            db,
            principal_id=account_id,
            capability=reserved_capability,
            scope=Scope.platform(),
            actor=actor,
            reason="ordinary grant must not confer a reserved capability",
            dependencies=dependencies,
        )

    with authorization_rw_engine.begin() as db:
        decision = authorize(
            db,
            raw_token=token,
            capability=reserved_capability,
            scope=Scope.platform(),
            dependencies=dependencies,
            decision_dependencies=_decision_dependencies(authorization_identity_engine),
        )
    assert decision.code is DecisionCode.DENIED

    with authorization_rw_engine.begin() as db:
        resolved = resolve_principal(
            db,
            raw_token=token,
            dependencies=dependencies,
            decision_dependencies=_decision_dependencies(authorization_identity_engine),
        )
    assert resolved.principal is not None
    assert all(item.capability != reserved_capability for item in resolved.principal.capabilities)

    # The resource guard must independently enforce the same reserved-capability rule.
    principal = AuthorizationPrincipal(
        account_id=account_id,
        employee_id="00000001",
        name="Alice",
        is_super_admin=False,
        authorization_version=resolved.principal.authorization_version,
        capabilities=(),
    )
    with authorization_rw_engine.begin() as db:
        assert (
            principal_has_capability(
                db,
                principal=principal,
                capability=reserved_capability,
                scope=Scope.platform(),
                dependencies=dependencies,
                decision_dependencies=_decision_dependencies(authorization_identity_engine),
            )
            is False
        )


def test_current_super_admin_fact_confers_exact_v02_platform_capabilities_without_grants(
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
    dependencies = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        db.execute(
            text(
                'INSERT INTO "authorization".principal_version '
                "(account_id, version, fence_generation, updated_at) "
                "VALUES (:account_id, 1, 0, now())"
            ),
            {"account_id": account_id},
        )

    decision_dependencies = _decision_dependencies(authorization_identity_engine)
    with authorization_rw_engine.begin() as db:
        resolved = resolve_principal(
            db,
            raw_token=token,
            dependencies=dependencies,
            decision_dependencies=decision_dependencies,
        )
    assert resolved.principal is not None
    assert V02_SUPER_ADMIN_PLATFORM_CAPABILITIES == EXPECTED_V02_SUPER_ADMIN_CAPABILITIES
    assert {
        (item.capability, item.scope.scope_type.value, item.scope.scope_id)
        for item in resolved.principal.capabilities
    } == {(capability, "PLATFORM", None) for capability in EXPECTED_V02_SUPER_ADMIN_CAPABILITIES}

    for capability in EXPECTED_V02_SUPER_ADMIN_CAPABILITIES:
        with authorization_rw_engine.begin() as db:
            decision = authorize(
                db,
                raw_token=token,
                capability=capability,
                scope=Scope.platform(),
                dependencies=dependencies,
                decision_dependencies=decision_dependencies,
            )
        assert decision.code is DecisionCode.ALLOW
        assert decision.principal is not None
        with authorization_rw_engine.begin() as db:
            assert principal_has_capability(
                db,
                principal=decision.principal,
                capability=capability,
                scope=Scope.platform(),
                dependencies=dependencies,
                decision_dependencies=decision_dependencies,
            )

    with authorization_rw_engine.begin() as db:
        future = authorize(
            db,
            raw_token=token,
            capability="platform.future.manage",
            scope=Scope.platform(),
            dependencies=dependencies,
            decision_dependencies=decision_dependencies,
        )
    assert future.code is DecisionCode.DENIED
    with authorization_rw_engine.begin() as db:
        assert (
            principal_has_capability(
                db,
                principal=resolved.principal,
                capability="platform.future.manage",
                scope=Scope.platform(),
                dependencies=dependencies,
                decision_dependencies=decision_dependencies,
            )
            is False
        )

    with authorization_rw_engine.begin() as db:
        workspace_scoped = authorize(
            db,
            raw_token=token,
            capability="platform.workspace.manage",
            scope=Scope.workspace("workspace-1"),
            dependencies=dependencies,
            decision_dependencies=decision_dependencies,
        )
    assert workspace_scoped.code is DecisionCode.DENIED
    with authorization_rw_engine.begin() as db:
        assert (
            principal_has_capability(
                db,
                principal=resolved.principal,
                capability="platform.workspace.manage",
                scope=Scope.workspace("workspace-1"),
                dependencies=dependencies,
                decision_dependencies=decision_dependencies,
            )
            is False
        )


@pytest.mark.parametrize(
    ("identity_mutation", "expected_code"),
    [
        (
            "UPDATE identity.account SET is_super_admin=false, version=version+1 "
            "WHERE id=:account_id",
            DecisionCode.DENIED,
        ),
        (
            "UPDATE identity.account SET status='DISABLED', version=version+1 WHERE id=:account_id",
            DecisionCode.UNAUTHENTICATED,
        ),
        (
            "UPDATE identity.session SET revoked_at=now(), revoke_reason='TEST' "
            "WHERE account_id=:account_id AND kind='FULL'",
            DecisionCode.UNAUTHENTICATED,
        ),
    ],
    ids=["super-admin-revoked", "account-disabled", "session-revoked"],
)
def test_super_admin_auto_capability_requires_current_identity_state(
    identity_mutation: str,
    expected_code: DecisionCode,
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id, token = _initialize_current_super_admin(
        identity_engine=authorization_identity_engine,
        authorization_engine=authorization_rw_engine,
        owner_engine=authorization_owner_engine,
        monkeypatch=monkeypatch,
    )
    dependencies = authorization_dependencies()
    decision_dependencies = _decision_dependencies(authorization_identity_engine)
    with authorization_rw_engine.begin() as db:
        initial = authorize(
            db,
            raw_token=token,
            capability="platform.home.read",
            scope=Scope.platform(),
            dependencies=dependencies,
            decision_dependencies=decision_dependencies,
        )
    assert initial.code is DecisionCode.ALLOW

    with authorization_owner_engine.begin() as db:
        db.execute(text(identity_mutation), {"account_id": account_id})
    with authorization_rw_engine.begin() as db:
        after_identity_change = authorize(
            db,
            raw_token=token,
            capability="platform.home.read",
            scope=Scope.platform(),
            dependencies=dependencies,
            decision_dependencies=decision_dependencies,
        )
    assert after_identity_change.allowed is False
    assert after_identity_change.code is expected_code


def test_super_admin_resource_guard_rejects_a_stale_authorization_version(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id, token = _initialize_current_super_admin(
        identity_engine=authorization_identity_engine,
        authorization_engine=authorization_rw_engine,
        owner_engine=authorization_owner_engine,
        monkeypatch=monkeypatch,
    )
    dependencies = authorization_dependencies()
    decision_dependencies = _decision_dependencies(authorization_identity_engine)
    with authorization_rw_engine.begin() as db:
        initial = authorize(
            db,
            raw_token=token,
            capability="platform.home.read",
            scope=Scope.platform(),
            dependencies=dependencies,
            decision_dependencies=decision_dependencies,
        )
    assert initial.principal is not None

    with authorization_rw_engine.begin() as db:
        db.execute(
            text(
                'UPDATE "authorization".principal_version '
                "SET version=version+1, updated_at=now() WHERE account_id=:account_id"
            ),
            {"account_id": account_id},
        )
    with authorization_rw_engine.begin() as db:
        with pytest.raises(AuthorizationUnavailable, match="principal changed"):
            principal_has_capability(
                db,
                principal=initial.principal,
                capability="platform.home.read",
                scope=Scope.platform(),
                dependencies=dependencies,
                decision_dependencies=decision_dependencies,
            )


def test_super_admin_projection_failure_returns_unavailable(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _account_id, token = _initialize_current_super_admin(
        identity_engine=authorization_identity_engine,
        authorization_engine=authorization_rw_engine,
        owner_engine=authorization_owner_engine,
        monkeypatch=monkeypatch,
    )

    def fail_effective_grants(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("injected projection outage")

    monkeypatch.setattr(
        SqlAlchemyAuthorizationRepository,
        "effective_grants",
        fail_effective_grants,
    )
    with authorization_rw_engine.begin() as db:
        unavailable = authorize(
            db,
            raw_token=token,
            capability="platform.home.read",
            scope=Scope.platform(),
            dependencies=authorization_dependencies(),
            decision_dependencies=_decision_dependencies(authorization_identity_engine),
        )
    assert unavailable.allowed is False
    assert unavailable.code is DecisionCode.UNAVAILABLE


def test_super_admin_version_repository_failure_is_unavailable_for_both_guards(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    authorization_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _account_id, token = _initialize_current_super_admin(
        identity_engine=authorization_identity_engine,
        authorization_engine=authorization_rw_engine,
        owner_engine=authorization_owner_engine,
        monkeypatch=monkeypatch,
    )
    dependencies = authorization_dependencies()
    decision_dependencies = _decision_dependencies(authorization_identity_engine)
    with authorization_rw_engine.begin() as db:
        initial = authorize(
            db,
            raw_token=token,
            capability="platform.home.read",
            scope=Scope.platform(),
            dependencies=dependencies,
            decision_dependencies=decision_dependencies,
        )
    assert initial.principal is not None

    def fail_principal_version(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected repository outage")

    monkeypatch.setattr(
        SqlAlchemyAuthorizationRepository,
        "principal_version",
        fail_principal_version,
    )
    with authorization_rw_engine.begin() as db:
        unavailable = authorize(
            db,
            raw_token=token,
            capability="platform.home.read",
            scope=Scope.platform(),
            dependencies=dependencies,
            decision_dependencies=decision_dependencies,
        )
    assert unavailable.allowed is False
    assert unavailable.code is DecisionCode.UNAVAILABLE

    with authorization_rw_engine.begin() as db:
        with pytest.raises(AuthorizationUnavailable, match="version unavailable"):
            principal_has_capability(
                db,
                principal=initial.principal,
                capability="platform.home.read",
                scope=Scope.platform(),
                dependencies=dependencies,
                decision_dependencies=decision_dependencies,
            )
