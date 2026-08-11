from dataclasses import dataclass, field
from datetime import timedelta

import pyotp
import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.identity import (
    AccountStatus,
    EffectiveIdentityPolicy,
    IdentityDependencies,
    InvalidAccountTransition,
    LastEffectiveSuperAdmin,
    LoginChallenge,
    Principal,
    SessionKind,
    StaleAccountVersion,
    complete_password_setup,
    confirm_totp,
    consume_temp_password,
    create_account,
    enroll_totp,
    login_password_step,
    login_totp_step,
    logout,
    revoke_sessions_for,
    set_account_status,
    validate_session,
)
from control_plane.app.modules.identity.adapters.runtime import SystemRandom
from tests.identity.task5_helpers import MutableClock, StaticSecrets, dependencies
from tests.identity.test_auth_flow import VALID_PASSWORD, _initialize_account

pytestmark = pytest.mark.integration

SYSTEM = Principal(employee_id="SYSTEM", name="System")


@dataclass
class AuthChanges:
    account_ids: list[str] = field(default_factory=list)

    def __call__(self, account_id: str) -> None:
        self.account_ids.append(account_id)


class TightSessionPolicy:
    def get_identity_policy(self, db: object) -> EffectiveIdentityPolicy:
        del db
        return EffectiveIdentityPolicy(
            session_cap=1,
            session_idle_timeout=timedelta(minutes=15),
        )


def _login_full(
    engine: Engine,
    deps: IdentityDependencies,
    secret: str,
) -> str:
    with engine.begin() as db:
        challenge = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="127.0.0.1",
            dependencies=deps,
        )
    assert isinstance(challenge, LoginChallenge)
    code = pyotp.TOTP(secret).at(deps.clock.now())
    with engine.begin() as db:
        issued = login_totp_step(
            db,
            challenge_token=challenge.challenge_token,
            code=code,
            dependencies=deps,
        )
    return issued.raw_token


def test_validate_session_separates_bootstrap_and_full_principals(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
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
    with identity_rw_engine.begin() as db:
        bootstrap = consume_temp_password(
            db,
            employee_no="00000001",
            temp_password=temporary_password,
            dependencies=deps,
        )
    assert bootstrap is not None
    with identity_rw_engine.begin() as db:
        restricted = validate_session(db, raw_token=bootstrap.raw_token, dependencies=deps)
    assert restricted is not None
    assert restricted.session_kind is SessionKind.BOOTSTRAP

    with identity_rw_engine.begin() as db:
        complete_password_setup(
            db,
            bootstrap_token=bootstrap.raw_token,
            password=VALID_PASSWORD,
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db:
        enrollment = enroll_totp(
            db,
            bootstrap_token=bootstrap.raw_token,
            dependencies=deps,
        )
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: deps.clock.now().timestamp(),
    )
    with identity_rw_engine.begin() as db:
        confirmed = confirm_totp(
            db,
            bootstrap_token=bootstrap.raw_token,
            code=pyotp.TOTP(enrollment.secret).at(deps.clock.now()),
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db:
        full = validate_session(db, raw_token=confirmed.raw_token, dependencies=deps)
    assert full is not None
    assert full.session_kind is SessionKind.FULL


def test_session_cap_evicts_oldest_full_session_deterministically(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = dependencies(clock=clock)
    secret, first = _initialize_account(identity_rw_engine, deps, monkeypatch)
    tokens = [first]
    for _ in range(3):
        clock.value += timedelta(seconds=30)
        tokens.append(_login_full(identity_rw_engine, deps, secret))

    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=tokens[0], dependencies=deps) is None
        assert all(
            validate_session(db, raw_token=token, dependencies=deps) is not None
            for token in tokens[1:]
        )


def test_policy_changes_session_cap_and_idle_boundary(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        secret_manager=StaticSecrets(),
        policy=TightSessionPolicy(),
        clock=clock,
        random=SystemRandom(),
    )
    secret, first = _initialize_account(identity_rw_engine, deps, monkeypatch)
    clock.value += timedelta(seconds=30)
    second = _login_full(identity_rw_engine, deps, secret)
    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=first, dependencies=deps) is None
        assert validate_session(db, raw_token=second, dependencies=deps) is not None

    clock.value += timedelta(minutes=15)
    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=second, dependencies=deps) is None


def test_logout_and_bulk_revocation_are_update_only_and_invoke_auth_hook(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changes = AuthChanges()
    deps = IdentityDependencies(
        secret_manager=StaticSecrets(),
        policy=dependencies().policy,
        clock=MutableClock(),
        random=SystemRandom(),
        on_auth_change=changes,
    )
    _, token = _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())
    with identity_rw_engine.begin() as db:
        assert logout(db, raw_token=token, dependencies=deps) is True
    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=token, dependencies=deps) is None

    with identity_rw_engine.begin() as db:
        revoked = revoke_sessions_for(
            db,
            account_id=account_id,
            actor=SYSTEM,
            reason="security event",
            dependencies=deps,
        )
    assert revoked == 0
    assert changes.account_ids[-2:] == [account_id, account_id]


def test_status_change_rejects_stale_version_revokes_and_guards_last_super_admin(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changes = AuthChanges()
    deps = IdentityDependencies(
        secret_manager=StaticSecrets(),
        policy=dependencies().policy,
        clock=MutableClock(),
        random=SystemRandom(),
        on_auth_change=changes,
    )
    _, token = _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.begin() as db:
        account = db.execute(
            text(
                "UPDATE identity.account SET is_super_admin=true, version=version+1 "
                "RETURNING id, version"
            )
        ).one()
    with identity_rw_engine.begin() as db, pytest.raises(StaleAccountVersion):
        set_account_status(
            db,
            account_id=str(account.id),
            status=AccountStatus.DISABLED,
            expected_version=account.version - 1,
            actor=SYSTEM,
            reason="stale disable",
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db, pytest.raises(LastEffectiveSuperAdmin):
        set_account_status(
            db,
            account_id=str(account.id),
            status=AccountStatus.DISABLED,
            expected_version=account.version,
            actor=SYSTEM,
            reason="disable",
            dependencies=deps,
        )

    with identity_rw_engine.begin() as db:
        db.execute(text("UPDATE identity.account SET is_super_admin=false"))
        updated = set_account_status(
            db,
            account_id=str(account.id),
            status=AccountStatus.DISABLED,
            expected_version=account.version,
            actor=SYSTEM,
            reason="disable",
            dependencies=deps,
        )
    assert updated.status is AccountStatus.DISABLED
    with identity_rw_engine.begin() as db:
        assert validate_session(db, raw_token=token, dependencies=deps) is None
    assert changes.account_ids[-1] == str(account.id)


def test_status_change_cannot_enable_an_uninitialized_account(
    clean_identity_db: None,
    identity_rw_engine: Engine,
) -> None:
    deps = dependencies()
    with identity_rw_engine.begin() as db:
        account, _ = create_account(
            db,
            employee_no="00000001",
            display_name="Alice",
            actor=SYSTEM,
            reason="provision",
            dependencies=deps,
        )

    with identity_rw_engine.begin() as db, pytest.raises(InvalidAccountTransition):
        set_account_status(
            db,
            account_id=account.id,
            status=AccountStatus.ENABLED,
            expected_version=account.version,
            actor=SYSTEM,
            reason="unsafe enable",
            dependencies=deps,
        )

    with identity_rw_engine.connect() as db:
        status = db.execute(
            text("SELECT status FROM identity.account WHERE id=:id"),
            {"id": account.id},
        ).scalar_one()
    assert status == AccountStatus.PENDING_INIT.value
