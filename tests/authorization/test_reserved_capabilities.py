from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.authorization import (
    PLATFORM_CONFIGURATION_MANAGE,
    PLATFORM_SUPER_ADMIN_MANAGE,
    AuthorizationPrincipal,
    DecisionCode,
    DecisionDependencies,
    Scope,
    authorize,
    grant,
    principal_has_capability,
    resolve_principal,
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


def test_current_super_admin_fact_confers_both_reserved_capabilities_without_grants(
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
    assert {
        (item.capability, item.scope.scope_type.value, item.scope.scope_id)
        for item in resolved.principal.capabilities
    } == {
        (PLATFORM_CONFIGURATION_MANAGE, "PLATFORM", None),
        (PLATFORM_SUPER_ADMIN_MANAGE, "PLATFORM", None),
    }

    for reserved_capability in (
        PLATFORM_CONFIGURATION_MANAGE,
        PLATFORM_SUPER_ADMIN_MANAGE,
    ):
        with authorization_rw_engine.begin() as db:
            decision = authorize(
                db,
                raw_token=token,
                capability=reserved_capability,
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
                capability=reserved_capability,
                scope=Scope.platform(),
                dependencies=dependencies,
                decision_dependencies=decision_dependencies,
            )
