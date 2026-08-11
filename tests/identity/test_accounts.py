from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.identity import (
    AccountStatus,
    EffectiveIdentityPolicy,
    IdentityDependencies,
    LastEffectiveSuperAdmin,
    Principal,
    SessionKind,
    consume_temp_password,
    create_account,
    issue_temp_password,
    validate_session,
)
from control_plane.app.modules.identity.adapters.runtime import SystemRandom
from tests.identity.task5_helpers import MutableClock, StaticSecrets, dependencies
from tests.identity.test_auth_flow import _initialize_account

pytestmark = pytest.mark.integration

SYSTEM = Principal(employee_id="SYSTEM", name="System")


class ShortTempPolicy:
    def get_identity_policy(self, db: object) -> EffectiveIdentityPolicy:
        del db
        return EffectiveIdentityPolicy(temp_credential_ttl=timedelta(minutes=5))


def test_create_account_returns_temp_once_without_persisting_or_auditing_plaintext(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    with identity_rw_engine.begin() as db:
        account, temporary_password = create_account(
            db,
            employee_no="00000001",
            display_name="Alice",
            actor=SYSTEM,
            reason="provision",
            dependencies=dependencies(),
        )

    assert account.employee_no == "00000001"
    assert account.status is AccountStatus.PENDING_INIT
    assert len(temporary_password) >= 24
    with identity_owner_engine.connect() as db:
        stored = db.execute(
            text(
                "SELECT a.password_hash, t.secret_hash FROM identity.account a "
                "JOIN identity.temp_credential t ON t.account_id=a.id"
            )
        ).one()
        audit_rows = db.execute(
            text("SELECT action, reason FROM audit.audit_event ORDER BY occurred_at")
        ).all()

    assert stored.password_hash is None
    assert stored.secret_hash != temporary_password
    assert all(temporary_password not in str(row) for row in audit_rows)
    assert {row.action for row in audit_rows} == {
        "identity.account.created",
        "identity.temp_credential.issued",
    }


def test_issuing_new_temp_password_invalidates_the_previous_one(
    clean_identity_db: None,
    identity_rw_engine: Engine,
) -> None:
    deps = dependencies()
    with identity_rw_engine.begin() as db:
        account, first = create_account(
            db,
            employee_no="00000001",
            display_name="Alice",
            actor=SYSTEM,
            reason="provision",
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db:
        second = issue_temp_password(
            db,
            account_id=account.id,
            actor=SYSTEM,
            reason="reset",
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db:
        assert (
            consume_temp_password(
                db,
                employee_no="00000001",
                temp_password=first,
                dependencies=deps,
            )
            is None
        )
    with identity_rw_engine.begin() as db:
        issued = consume_temp_password(
            db,
            employee_no="00000001",
            temp_password=second,
            dependencies=deps,
        )

    assert issued is not None
    assert issued.kind is SessionKind.BOOTSTRAP


def test_temp_credential_records_the_actual_local_issuer(
    clean_identity_db: None,
    identity_rw_engine: Engine,
) -> None:
    deps = dependencies()
    with identity_rw_engine.begin() as db:
        actor_account, _ = create_account(
            db,
            employee_no="00000001",
            display_name="Admin",
            actor=SYSTEM,
            reason="bootstrap",
            dependencies=deps,
        )
    actor = Principal(employee_id="00000001", name="Admin")
    with identity_rw_engine.begin() as db:
        target, _ = create_account(
            db,
            employee_no="00000002",
            display_name="Alice",
            actor=actor,
            reason="provision",
            dependencies=deps,
        )
    with identity_rw_engine.connect() as db:
        issued_by = db.execute(
            text("SELECT issued_by FROM identity.temp_credential WHERE account_id=:account_id"),
            {"account_id": target.id},
        ).scalar_one()

    assert str(issued_by) == actor_account.id


def test_temp_password_expiry_uses_effective_policy(
    clean_identity_db: None,
    identity_rw_engine: Engine,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        secret_manager=StaticSecrets(),
        policy=ShortTempPolicy(),
        clock=clock,
        random=SystemRandom(),
    )
    with identity_rw_engine.begin() as db:
        _, temporary_password = create_account(
            db,
            employee_no="00000001",
            display_name="Alice",
            actor=SYSTEM,
            reason="provision",
            dependencies=deps,
        )
    clock.value += timedelta(minutes=5)
    with identity_rw_engine.begin() as db:
        result = consume_temp_password(
            db,
            employee_no="00000001",
            temp_password=temporary_password,
            dependencies=deps,
        )

    assert result is None


def test_two_connections_racing_one_temp_password_have_exactly_one_success(
    clean_identity_db: None,
    identity_rw_engine: Engine,
) -> None:
    deps = dependencies()
    with identity_rw_engine.begin() as db:
        _, temporary_password = create_account(
            db,
            employee_no="00000001",
            display_name="Alice",
            actor=SYSTEM,
            reason="provision",
            dependencies=deps,
        )
    barrier = Barrier(2)

    def consume() -> bool:
        with identity_rw_engine.begin() as db:
            barrier.wait(timeout=5)
            return (
                consume_temp_password(
                    db,
                    employee_no="00000001",
                    temp_password=temporary_password,
                    dependencies=deps,
                )
                is not None
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=10) for future in [executor.submit(consume) for _ in range(2)]
        ]

    assert sorted(outcomes) == [False, True]


def test_domain_and_audit_are_rolled_back_together(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    with identity_rw_engine.connect() as db:
        transaction = db.begin()
        create_account(
            db,
            employee_no="00000001",
            display_name="Alice",
            actor=SYSTEM,
            reason="rollback proof",
            dependencies=dependencies(),
        )
        transaction.rollback()

    with identity_owner_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity.account")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM audit.audit_event")).scalar_one() == 0


def test_password_reset_invalidates_formal_password_and_sessions_but_preserves_totp(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = dependencies()
    _, old_session = _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.connect() as db:
        before = db.execute(
            text("SELECT id, totp_sealed, totp_confirmed_at FROM identity.account")
        ).one()
    with identity_rw_engine.begin() as db:
        issue_temp_password(
            db,
            account_id=str(before.id),
            actor=SYSTEM,
            reason="reset",
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=old_session, dependencies=deps) is None
    with identity_rw_engine.connect() as db:
        after = db.execute(
            text(
                "SELECT status, password_hash, password_set_at, "
                "totp_sealed, totp_confirmed_at FROM identity.account"
            )
        ).one()
    with identity_owner_engine.connect() as db:
        actions = set(db.execute(text("SELECT action FROM audit.audit_event")).scalars())

    assert after.status == "PENDING_INIT"
    assert after.password_hash is None
    assert after.password_set_at is None
    assert after.totp_sealed == before.totp_sealed
    assert after.totp_confirmed_at == before.totp_confirmed_at
    assert {
        "identity.password.reset",
        "identity.sessions.revoked",
        "identity.temp_credential.issued",
    } <= actions


def test_password_reset_cannot_make_the_last_effective_super_admin_unavailable(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = dependencies()
    _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.begin() as db:
        account = db.execute(
            text("UPDATE identity.account SET is_super_admin=true, version=version+1 RETURNING id")
        ).one()
    with identity_rw_engine.begin() as db, pytest.raises(LastEffectiveSuperAdmin):
        issue_temp_password(
            db,
            account_id=str(account.id),
            actor=SYSTEM,
            reason="unsafe reset",
            dependencies=deps,
        )
    with identity_rw_engine.connect() as db:
        state = db.execute(
            text("SELECT status, password_hash FROM identity.account WHERE id=:id"),
            {"id": account.id},
        ).one()

    assert state.status == "ENABLED"
    assert state.password_hash is not None
