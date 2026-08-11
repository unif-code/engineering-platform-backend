from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.authorization import (
    InvalidGrant,
    Scope,
    StaleGrantVersion,
    effective_grants,
    grant,
    revoke,
)
from control_plane.app.modules.identity import (
    BootstrapPurpose,
    SessionKind,
    SessionPrincipal,
)
from tests.authorization.helpers import MutableClock, authorization_dependencies

pytestmark = pytest.mark.integration


def _actor() -> SessionPrincipal:
    return SessionPrincipal(
        account_id="00000000-0000-0000-0000-000000000900",
        employee_no="00000900",
        display_name="Grant Admin",
        session_kind=SessionKind.FULL,
        bootstrap_purpose=BootstrapPurpose.INITIAL_SETUP,
        is_super_admin=False,
    )


def test_grant_create_is_atomic_with_audit_and_principal_version(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_owner_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        created = grant(
            db,
            principal_id="account-1",
            capability="platform.organization.read",
            scope=Scope.platform(),
            actor=_actor(),
            reason="read organization",
            dependencies=deps,
        )

    assert created.version == 1
    assert created.scope == Scope.platform()
    with authorization_owner_engine.connect() as db:
        version = db.execute(
            text(
                'SELECT version FROM "authorization".principal_version '
                "WHERE account_id='account-1'"
            )
        ).scalar_one()
        audit = db.execute(
            text(
                "SELECT actor, action, target_id, reason FROM audit.audit_event "
                "WHERE action='authorization.grant.created'"
            )
        ).one()
    assert version == 2
    assert audit[0] == _actor().account_id
    assert audit[2] == created.id
    assert "read organization" in audit[3]


def test_effective_grants_use_half_open_time_and_exact_scope(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    clock = MutableClock()
    deps = authorization_dependencies(clock)
    with authorization_rw_engine.begin() as db:
        grant(
            db,
            principal_id="account-1",
            capability="workspace.members.read",
            scope=Scope.workspace("workspace-1"),
            actor=_actor(),
            reason="valid",
            valid_from=clock.value,
            valid_to=clock.value + timedelta(hours=1),
            dependencies=deps,
        )
        grant(
            db,
            principal_id="account-1",
            capability="workspace.members.read",
            scope=Scope.workspace("workspace-2"),
            actor=_actor(),
            reason="future",
            valid_from=clock.value + timedelta(minutes=1),
            dependencies=deps,
        )

    with authorization_rw_engine.connect() as db:
        exact = effective_grants(
            db,
            principal_id="account-1",
            capability="workspace.members.read",
            scope=Scope.workspace("workspace-1"),
            dependencies=deps,
        )
        wrong = effective_grants(
            db,
            principal_id="account-1",
            capability="workspace.members.read",
            scope=Scope.workspace("workspace-2"),
            dependencies=deps,
        )
    assert len(exact) == 1
    assert wrong == []

    clock.value += timedelta(hours=1)
    with authorization_rw_engine.connect() as db:
        expired = effective_grants(
            db,
            principal_id="account-1",
            capability="workspace.members.read",
            scope=Scope.workspace("workspace-1"),
            dependencies=deps,
        )
    assert expired == []


def test_revoke_is_versioned_preserves_history_and_denies_immediately(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        created = grant(
            db,
            principal_id="account-1",
            capability="platform.organization.read",
            scope=Scope.platform(),
            actor=_actor(),
            reason="temporary",
            dependencies=deps,
        )
    with authorization_rw_engine.begin() as db:
        revoked = revoke(
            db,
            grant_id=created.id,
            expected_version=1,
            actor=_actor(),
            reason="remove access",
            dependencies=deps,
        )
    assert revoked.status == "REVOKED"
    assert revoked.version == 2
    with authorization_rw_engine.connect() as db:
        assert (
            effective_grants(
                db,
                principal_id="account-1",
                capability="platform.organization.read",
                scope=Scope.platform(),
                dependencies=deps,
            )
            == []
        )
    with authorization_rw_engine.begin() as db, pytest.raises(StaleGrantVersion):
        revoke(
            db,
            grant_id=created.id,
            expected_version=1,
            actor=_actor(),
            reason="stale retry",
            dependencies=deps,
        )


def test_revoke_rejects_blank_reason_without_mutating_history(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_owner_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        created = grant(
            db,
            principal_id="account-1",
            capability="platform.organization.read",
            scope=Scope.platform(),
            actor=_actor(),
            reason="temporary",
            dependencies=deps,
        )

    with authorization_rw_engine.begin() as db, pytest.raises(InvalidGrant):
        revoke(
            db,
            grant_id=created.id,
            expected_version=1,
            actor=_actor(),
            reason="   ",
            dependencies=deps,
        )

    with authorization_owner_engine.connect() as db:
        row = db.execute(
            text('SELECT status, version FROM "authorization"."grant" WHERE id=:id'),
            {"id": created.id},
        ).one()
        revoked_audits = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event WHERE action='authorization.grant.revoked'"
            )
        ).scalar_one()
    assert row == ("ACTIVE", 1)
    assert revoked_audits == 0
