from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pyotp
import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.identity import (
    AuthenticationFailed,
    EffectiveIdentityPolicy,
    IdentityDependencies,
    LoginBackoffActive,
    LoginChallenge,
    PasswordFloorViolation,
    Principal,
    SessionKind,
    TotpChallengeFailed,
    complete_password_setup,
    confirm_totp,
    consume_temp_password,
    create_account,
    enroll_totp,
    login_password_step,
    login_totp_step,
)
from control_plane.app.modules.identity.adapters.runtime import SystemRandom
from tests.identity.task5_helpers import MutableClock, StaticSecrets, dependencies

pytestmark = pytest.mark.integration

SYSTEM = Principal(employee_id="SYSTEM", name="System")
VALID_PASSWORD = "Str0ng!Secure#2026"


class ExpiringPasswordPolicy:
    def get_identity_policy(self, db: object) -> EffectiveIdentityPolicy:
        del db
        return EffectiveIdentityPolicy(password_max_age=timedelta(days=90))


class TightAuthPolicy:
    def get_identity_policy(self, db: object) -> EffectiveIdentityPolicy:
        del db
        return EffectiveIdentityPolicy(
            backoff_threshold=2,
            backoff_initial_delay=timedelta(seconds=30),
            totp_attempt_cap=2,
        )


class IdleBootstrapPolicy:
    def get_identity_policy(self, db: object) -> EffectiveIdentityPolicy:
        del db
        return EffectiveIdentityPolicy(session_idle_timeout=timedelta(minutes=15))


def _start_bootstrap(identity_rw_engine: Engine, deps: IdentityDependencies) -> str:
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
        issued = consume_temp_password(
            db,
            employee_no="00000001",
            temp_password=temporary_password,
            dependencies=deps,
        )
    assert issued is not None
    return issued.raw_token


def _initialize_account(
    identity_rw_engine: Engine,
    deps: IdentityDependencies,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    bootstrap_token = _start_bootstrap(identity_rw_engine, deps)
    with identity_rw_engine.begin() as db:
        complete_password_setup(
            db,
            bootstrap_token=bootstrap_token,
            password=VALID_PASSWORD,
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db:
        enrollment = enroll_totp(
            db,
            bootstrap_token=bootstrap_token,
            dependencies=deps,
        )
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: deps.clock.now().timestamp(),
    )
    code = pyotp.TOTP(enrollment.secret).at(deps.clock.now())
    with identity_rw_engine.begin() as db:
        full = confirm_totp(
            db,
            bootstrap_token=bootstrap_token,
            code=code,
            dependencies=deps,
        )
    assert full.kind is SessionKind.FULL
    return enrollment.secret, full.raw_token


def test_password_setup_enforces_floor_and_sets_server_timestamp_only_on_success(
    clean_identity_db: None,
    identity_rw_engine: Engine,
) -> None:
    clock = MutableClock()
    deps = dependencies(clock=clock)
    bootstrap_token = _start_bootstrap(identity_rw_engine, deps)
    with identity_rw_engine.begin() as db, pytest.raises(PasswordFloorViolation):
        complete_password_setup(
            db,
            bootstrap_token=bootstrap_token,
            password="Password!2026aaaa",
            dependencies=deps,
        )
    with identity_rw_engine.connect() as db:
        assert db.execute(text("SELECT password_set_at FROM identity.account")).scalar_one() is None

    with identity_rw_engine.begin() as db:
        complete_password_setup(
            db,
            bootstrap_token=bootstrap_token,
            password=VALID_PASSWORD,
            dependencies=deps,
        )
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(text("SELECT password_set_at FROM identity.account")).scalar_one()
            == clock.value
        )


def test_bootstrap_commands_reject_an_idle_expired_session(
    clean_identity_db: None,
    identity_rw_engine: Engine,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        secret_manager=StaticSecrets(),
        policy=IdleBootstrapPolicy(),
        clock=clock,
        random=SystemRandom(),
    )
    bootstrap_token = _start_bootstrap(identity_rw_engine, deps)
    clock.value += timedelta(minutes=15)

    with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
        complete_password_setup(
            db,
            bootstrap_token=bootstrap_token,
            password=VALID_PASSWORD,
            dependencies=deps,
        )

    with identity_rw_engine.connect() as db:
        state = db.execute(
            text(
                "SELECT s.revoked_at, a.password_set_at FROM identity.session s "
                "JOIN identity.account a ON a.id=s.account_id"
            )
        ).one()
    assert state.revoked_at == clock.value
    assert state.password_set_at is None


def test_totp_enrollment_returns_secret_once_and_confirmation_persists_used_step(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = dependencies()
    secret, _ = _initialize_account(identity_rw_engine, deps, monkeypatch)

    with identity_owner_engine.connect() as db:
        row = db.execute(
            text("SELECT totp_sealed, totp_last_step, status FROM identity.account")
        ).one()
        audit_rows = db.execute(text("SELECT action, reason FROM audit.audit_event")).all()

    assert row.totp_sealed != secret.encode()
    assert row.totp_last_step == int(deps.clock.now().timestamp()) // 30
    assert row.status == "ENABLED"
    assert all(secret not in str(audit_row) for audit_row in audit_rows)


def test_password_login_issues_purpose_bound_challenge_and_totp_replay_is_rejected(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = dependencies()
    secret, _ = _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.begin() as db:
        challenge = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="127.0.0.1",
            dependencies=deps,
        )
    assert isinstance(challenge, LoginChallenge)
    with identity_rw_engine.connect() as db:
        stored = db.execute(text("SELECT token_hash, purpose FROM identity.auth_challenge")).one()
    assert stored.token_hash != challenge.challenge_token
    assert stored.purpose == "LOGIN_TOTP"

    replayed_code = pyotp.TOTP(secret).at(deps.clock.now())
    with identity_rw_engine.begin() as db, pytest.raises(TotpChallengeFailed):
        login_totp_step(
            db,
            challenge_token=challenge.challenge_token,
            code=replayed_code,
            dependencies=deps,
        )


def test_totp_purpose_mismatch_and_five_failures_exhaust_same_challenge(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = dependencies()
    _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.begin() as db:
        challenge = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="127.0.0.1",
            dependencies=deps,
        )
        assert isinstance(challenge, LoginChallenge)
        db.execute(text("UPDATE identity.auth_challenge SET purpose='POLICY_PUBLISH'"))
    with identity_rw_engine.begin() as db, pytest.raises(TotpChallengeFailed):
        login_totp_step(
            db,
            challenge_token=challenge.challenge_token,
            code="000000",
            dependencies=deps,
        )
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(text("SELECT attempt_count FROM identity.auth_challenge")).scalar_one() == 1
        )

    with identity_rw_engine.begin() as db:
        db.execute(text("UPDATE identity.auth_challenge SET purpose='LOGIN_TOTP', attempt_count=0"))
    for _ in range(5):
        with identity_rw_engine.begin() as db, pytest.raises(TotpChallengeFailed):
            login_totp_step(
                db,
                challenge_token=challenge.challenge_token,
                code="000000",
                dependencies=deps,
            )
    with identity_rw_engine.connect() as db:
        row = db.execute(
            text("SELECT attempt_count, revoked_at FROM identity.auth_challenge")
        ).one()
    assert row.attempt_count == 5
    assert row.revoked_at is not None


def test_two_connections_racing_one_totp_challenge_have_exactly_one_success(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = dependencies(clock=clock)
    secret, _ = _initialize_account(identity_rw_engine, deps, monkeypatch)
    clock.value += timedelta(seconds=30)
    with identity_rw_engine.begin() as db:
        challenge = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="127.0.0.1",
            dependencies=deps,
        )
    assert isinstance(challenge, LoginChallenge)
    code = pyotp.TOTP(secret).at(clock.value)
    barrier = Barrier(2)

    def consume() -> bool:
        with identity_rw_engine.begin() as db:
            barrier.wait(timeout=5)
            try:
                login_totp_step(
                    db,
                    challenge_token=challenge.challenge_token,
                    code=code,
                    dependencies=deps,
                )
            except TotpChallengeFailed:
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=10) for future in [executor.submit(consume) for _ in range(2)]
        ]

    assert sorted(outcomes) == [False, True]
    with identity_rw_engine.connect() as db:
        row = db.execute(
            text("SELECT consumed_at, attempt_count FROM identity.auth_challenge")
        ).one()
    assert row.consumed_at is not None
    assert row.attempt_count == 0


def test_expired_password_enters_restricted_setup_without_rewriting_password_fact(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        secret_manager=StaticSecrets(),
        policy=ExpiringPasswordPolicy(),
        clock=clock,
        random=SystemRandom(),
    )
    _initialize_account(identity_rw_engine, deps, monkeypatch)
    original_set_at = clock.value
    clock.value += timedelta(days=90)

    with identity_rw_engine.begin() as db:
        result = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="127.0.0.1",
            dependencies=deps,
        )
    assert not isinstance(result, LoginChallenge)
    assert result.kind is SessionKind.BOOTSTRAP
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(text("SELECT password_set_at FROM identity.account")).scalar_one()
            == original_set_at
        )
        assert db.execute(text("SELECT count(*) FROM identity.auth_challenge")).scalar_one() == 0


def test_login_backoff_is_server_side_exponential_and_source_isolated(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = dependencies(clock=clock)
    _initialize_account(identity_rw_engine, deps, monkeypatch)
    for _ in range(5):
        with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
            login_password_step(
                db,
                employee_no="00000001",
                password="wrong",
                source="source-a",
                dependencies=deps,
            )
    with identity_rw_engine.begin() as db, pytest.raises(LoginBackoffActive) as blocked:
        login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-a",
            dependencies=deps,
        )
    assert blocked.value.retry_after_seconds == 30

    clock.value += timedelta(seconds=30)
    with identity_rw_engine.begin() as db:
        after_lock = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-a",
            dependencies=deps,
        )
    assert isinstance(after_lock, LoginChallenge)
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT failure_count FROM identity.login_backoff "
                    "WHERE employee_no='00000001' AND source='source-a'"
                )
            ).scalar_one()
            == 0
        )

    with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
        login_password_step(
            db,
            employee_no="00000001",
            password="wrong",
            source="source-c",
            dependencies=deps,
        )
    clock.value += timedelta(hours=24)
    with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
        login_password_step(
            db,
            employee_no="00000001",
            password="wrong",
            source="source-c",
            dependencies=deps,
        )
    with identity_rw_engine.connect() as db:
        reset_state = db.execute(
            text(
                "SELECT failure_count, locked_until FROM identity.login_backoff "
                "WHERE employee_no='00000001' AND source='source-c'"
            )
        ).one()
    assert reset_state.failure_count == 1
    assert reset_state.locked_until is None

    with identity_rw_engine.begin() as db:
        other_source = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-b",
            dependencies=deps,
        )
    assert isinstance(other_source, LoginChallenge)

    for attempt in range(1, 11):
        with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
            login_password_step(
                db,
                employee_no="00000001",
                password="wrong",
                source="source-a",
                dependencies=deps,
            )
        if 5 <= attempt < 10:
            delay_seconds = min(30 * (2 ** (attempt - 5)), 15 * 60)
            clock.value += timedelta(seconds=delay_seconds)
    with identity_rw_engine.connect() as db:
        locked_until = db.execute(
            text(
                "SELECT locked_until FROM identity.login_backoff "
                "WHERE employee_no='00000001' AND source='source-a'"
            )
        ).scalar_one()
    assert locked_until - clock.value == timedelta(minutes=15)


def test_effective_policy_changes_backoff_threshold_and_totp_attempt_cap(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        secret_manager=StaticSecrets(),
        policy=TightAuthPolicy(),
        clock=clock,
        random=SystemRandom(),
    )
    secret, _ = _initialize_account(identity_rw_engine, deps, monkeypatch)
    for _ in range(2):
        with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
            login_password_step(
                db,
                employee_no="00000001",
                password="wrong",
                source="source-a",
                dependencies=deps,
            )
    with identity_rw_engine.begin() as db, pytest.raises(LoginBackoffActive) as blocked:
        login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-a",
            dependencies=deps,
        )
    assert blocked.value.retry_after_seconds == 30

    with identity_rw_engine.begin() as db:
        challenge = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-b",
            dependencies=deps,
        )
    assert isinstance(challenge, LoginChallenge)
    valid_code = pyotp.TOTP(secret).at(clock.value)
    invalid_code = valid_code[:-1] + str((int(valid_code[-1]) + 1) % 10)
    for _ in range(2):
        with identity_rw_engine.begin() as db, pytest.raises(TotpChallengeFailed):
            login_totp_step(
                db,
                challenge_token=challenge.challenge_token,
                code=invalid_code,
                dependencies=deps,
            )
    with identity_rw_engine.connect() as db:
        state = db.execute(
            text("SELECT attempt_count, revoked_at FROM identity.auth_challenge")
        ).one()
    assert state.attempt_count == 2
    assert state.revoked_at is not None


def test_audit_payloads_exclude_all_replayable_authentication_material(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = dependencies()
    secret, session_token = _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.begin() as db:
        challenge = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="127.0.0.1",
            dependencies=deps,
        )
    assert isinstance(challenge, LoginChallenge)
    code = pyotp.TOTP(secret).at(deps.clock.now())
    with identity_owner_engine.connect() as db:
        audit_text = "\n".join(
            str(row)
            for row in db.execute(
                text(
                    "SELECT actor, action, target_type, target_id, result, reason, "
                    "correlation_id FROM audit.audit_event"
                )
            )
        )

    for replayable in (VALID_PASSWORD, secret, code, session_token, challenge.challenge_token):
        assert replayable not in audit_text
