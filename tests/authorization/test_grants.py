from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.authorization import (
    InitialProvisioningDenied,
    InvalidGrant,
    Scope,
    StaleGrantVersion,
    effective_grants,
    grant,
    provision_initial_admin_grants,
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


def test_initial_admin_provisioning_is_exact_idempotent_and_once_only(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_owner_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    principal_id = "00000000-0000-0000-0000-000000000001"

    with authorization_rw_engine.begin() as db:
        first = provision_initial_admin_grants(
            db,
            principal_id=principal_id,
            command_id="cli-bootstrap-command",
            dependencies=deps,
        )
    with authorization_rw_engine.begin() as db:
        replay = provision_initial_admin_grants(
            db,
            principal_id=principal_id,
            command_id="cli-bootstrap-command",
            dependencies=deps,
        )
    assert replay == first
    assert [item.capability for item in first] == [
        "audit.read",
        "identity.account.manage",
        "platform.authorization.manage",
    ]
    assert all(
        item.scope == Scope.platform() and item.source == "SYSTEM_BOOTSTRAP" for item in first
    )

    with authorization_rw_engine.begin() as db:
        with pytest.raises(InitialProvisioningDenied):
            provision_initial_admin_grants(
                db,
                principal_id="00000000-0000-0000-0000-000000000002",
                command_id="cli-other-command",
                dependencies=deps,
            )

    with authorization_owner_engine.connect() as db:
        evidence = db.execute(
            text(
                "SELECT "
                '(SELECT count(*) FROM "authorization"."grant") AS grants, '
                '(SELECT count(*) FROM "authorization".idempotency_record '
                "WHERE operation='initial_admin_provisioning') AS claims, "
                "(SELECT count(*) FROM audit.audit_event "
                "WHERE action='authorization.grant.created' AND result='SUCCESS') AS created, "
                "(SELECT count(*) FROM audit.audit_event "
                "WHERE action='authorization.initial_provisioning' AND result='DENIED') AS denied, "
                "(SELECT count(*) FROM audit.audit_event "
                "WHERE actor='SYSTEM_BOOTSTRAP' AND actor_type='SYSTEM' "
                "AND correlation_id IN "
                "('cli-bootstrap-command', 'cli-other-command')) AS correlated, "
                '(SELECT count(*) FROM "authorization".idempotency_record '
                "WHERE operation='initial_admin_provisioning' AND state='COMPLETED') AS completed"
            )
        ).one()
    assert evidence == (3, 2, 3, 1, 4, 2)


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
