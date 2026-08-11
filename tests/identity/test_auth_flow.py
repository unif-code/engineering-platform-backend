from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pyotp
import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.identity import (
    AuthChallengeState,
    AuthDenialCode,
    AuthenticationDenial,
    AuthenticationFailed,
    BootstrapPurpose,
    EffectiveIdentityPolicy,
    IdentityDependencies,
    IssuedSession,
    LoginChallenge,
    PasswordFloorViolation,
    Principal,
    SessionKind,
    complete_password_setup,
    confirm_totp,
    consume_temp_password,
    create_account,
    enroll_totp,
    issue_temp_password,
    login_password_step,
    login_totp_step,
)
from control_plane.app.modules.identity.adapters.runtime import SystemRandom
from control_plane.app.modules.identity.adapters.sqlalchemy import SqlAlchemyIdentityRepository
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


class OneSessionPolicy:
    def get_identity_policy(self, db: object) -> EffectiveIdentityPolicy:
        del db
        return EffectiveIdentityPolicy(session_cap=1)


def _start_bootstrap(
    identity_rw_engine: Engine,
    deps: IdentityDependencies,
    *,
    employee_no: str = "00000001",
    display_name: str = "Alice",
) -> str:
    with identity_rw_engine.begin() as db:
        _, temporary_password = create_account(
            db,
            employee_no=employee_no,
            display_name=display_name,
            actor=SYSTEM,
            reason="provision",
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db:
        issued = consume_temp_password(
            db,
            employee_no=employee_no,
            temp_password=temporary_password,
            dependencies=deps,
        )
    assert issued is not None
    return issued.raw_token


def _initialize_account(
    identity_rw_engine: Engine,
    deps: IdentityDependencies,
    monkeypatch: pytest.MonkeyPatch,
    *,
    employee_no: str = "00000001",
    display_name: str = "Alice",
) -> tuple[str, str]:
    bootstrap_token = _start_bootstrap(
        identity_rw_engine,
        deps,
        employee_no=employee_no,
        display_name=display_name,
    )
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
    identity_owner_engine: Engine,
) -> None:
    clock = MutableClock()
    changes: list[str] = []

    def record_change(account_id: str) -> None:
        changes.append(account_id)

    deps = IdentityDependencies(
        repository_factory=dependencies().repository_factory,
        secret_manager=StaticSecrets(),
        policy=IdleBootstrapPolicy(),
        clock=clock,
        random=SystemRandom(),
        audit=dependencies().audit,
        on_auth_change=record_change,
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
    with identity_owner_engine.connect() as db:
        revoke_audits = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='identity.sessions.revoked' AND reason='idle timeout'"
            )
        ).scalar_one()
    assert state.revoked_at == clock.value
    assert state.password_set_at is None
    assert revoke_audits == 1
    assert len(changes) == 1


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
    with identity_rw_engine.begin() as db:
        denied = login_totp_step(
            db,
            challenge_token=challenge.challenge_token,
            code=replayed_code,
            dependencies=deps,
        )
    assert isinstance(denied, AuthenticationDenial)
    assert denied.code is AuthDenialCode.INVALID_CHALLENGE
    assert denied.challenge_state is AuthChallengeState.RETRY_ALLOWED


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
    with identity_rw_engine.begin() as db:
        mismatch = login_totp_step(
            db,
            challenge_token=challenge.challenge_token,
            code="000000",
            dependencies=deps,
        )
    assert isinstance(mismatch, AuthenticationDenial)
    assert mismatch.code is AuthDenialCode.INVALID_CHALLENGE
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(text("SELECT attempt_count FROM identity.auth_challenge")).scalar_one() == 1
        )

    with identity_rw_engine.begin() as db:
        db.execute(text("UPDATE identity.auth_challenge SET purpose='LOGIN_TOTP', attempt_count=0"))
    outcomes: list[AuthenticationDenial] = []
    for _ in range(5):
        with identity_rw_engine.begin() as db:
            denied = login_totp_step(
                db,
                challenge_token=challenge.challenge_token,
                code="000000",
                dependencies=deps,
            )
        assert isinstance(denied, AuthenticationDenial)
        assert denied.code is AuthDenialCode.INVALID_CHALLENGE
        outcomes.append(denied)
    assert [outcome.challenge_state for outcome in outcomes] == [
        AuthChallengeState.RETRY_ALLOWED,
        AuthChallengeState.RETRY_ALLOWED,
        AuthChallengeState.RETRY_ALLOWED,
        AuthChallengeState.RETRY_ALLOWED,
        AuthChallengeState.TERMINAL,
    ]
    with identity_rw_engine.connect() as db:
        row = db.execute(
            text("SELECT attempt_count, revoked_at FROM identity.auth_challenge")
        ).one()
    assert row.attempt_count == 5
    assert row.revoked_at is not None


def test_missing_challenge_returns_same_safe_terminal_denial(
    clean_identity_db: None,
    identity_rw_engine: Engine,
) -> None:
    with identity_rw_engine.begin() as db:
        denied = login_totp_step(
            db,
            challenge_token="missing-challenge",
            code="000000",
            dependencies=dependencies(),
        )

    assert isinstance(denied, AuthenticationDenial)
    assert denied.code is AuthDenialCode.INVALID_CHALLENGE
    assert denied.challenge_state is AuthChallengeState.TERMINAL


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
            result = login_totp_step(
                db,
                challenge_token=challenge.challenge_token,
                code=code,
                dependencies=deps,
            )
            return isinstance(result, IssuedSession)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=10) for future in [executor.submit(consume) for _ in range(2)]
        ]

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1
    with identity_rw_engine.connect() as db:
        row = db.execute(
            text("SELECT consumed_at, attempt_count FROM identity.auth_challenge")
        ).one()
    assert row.consumed_at is not None
    assert row.attempt_count == 0


def test_totp_step_advance_reports_exactly_one_atomic_winner(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = dependencies(clock=clock)
    _initialize_account(identity_rw_engine, deps, monkeypatch)
    clock.value += timedelta(seconds=30)
    next_step = int(clock.value.timestamp()) // 30
    barrier = Barrier(2)

    def advance() -> bool:
        with identity_rw_engine.begin() as db:
            barrier.wait(timeout=5)
            return SqlAlchemyIdentityRepository(db).update_totp_step(
                str(db.execute(text("SELECT id FROM identity.account")).scalar_one()),
                next_step,
                clock.value,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=10) for future in [executor.submit(advance) for _ in range(2)]
        ]
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1


def test_two_different_challenges_cannot_reuse_the_same_totp_step(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = dependencies(clock=clock)
    secret, _ = _initialize_account(identity_rw_engine, deps, monkeypatch)
    clock.value += timedelta(seconds=30)
    challenges: list[LoginChallenge] = []
    for source in ("source-a", "source-b"):
        with identity_rw_engine.begin() as db:
            challenge = login_password_step(
                db,
                employee_no="00000001",
                password=VALID_PASSWORD,
                source=source,
                dependencies=deps,
            )
        assert isinstance(challenge, LoginChallenge)
        challenges.append(challenge)
    code = pyotp.TOTP(secret).at(clock.value)
    barrier = Barrier(2)

    def consume(challenge: LoginChallenge) -> IssuedSession | AuthenticationDenial:
        with identity_rw_engine.begin() as db:
            barrier.wait(timeout=5)
            return login_totp_step(
                db,
                challenge_token=challenge.challenge_token,
                code=code,
                dependencies=deps,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=10)
            for future in [executor.submit(consume, challenge) for challenge in challenges]
        ]
    assert sum(isinstance(value, IssuedSession) for value in outcomes) == 1
    assert sum(isinstance(value, AuthenticationDenial) for value in outcomes) == 1


def test_two_different_challenges_cannot_exceed_the_session_cap(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        repository_factory=dependencies().repository_factory,
        secret_manager=StaticSecrets(),
        policy=OneSessionPolicy(),
        clock=clock,
        random=SystemRandom(),
        audit=dependencies().audit,
        on_auth_change=dependencies().on_auth_change,
    )
    secret, _ = _initialize_account(identity_rw_engine, deps, monkeypatch)
    clock.value += timedelta(seconds=30)
    challenges: list[LoginChallenge] = []
    for source in ("source-a", "source-b"):
        with identity_rw_engine.begin() as db:
            challenge = login_password_step(
                db,
                employee_no="00000001",
                password=VALID_PASSWORD,
                source=source,
                dependencies=deps,
            )
        assert isinstance(challenge, LoginChallenge)
        challenges.append(challenge)
    codes = [
        pyotp.TOTP(secret).at(clock.value),
        pyotp.TOTP(secret).at(clock.value + timedelta(seconds=30)),
    ]
    with identity_rw_engine.connect() as db:
        account_id = str(db.execute(text("SELECT id FROM identity.account")).scalar_one())
    barrier = Barrier(2)

    def consume(index: int) -> IssuedSession | AuthenticationDenial:
        with identity_rw_engine.begin() as db:
            if index == 0:
                db.execute(
                    text("SELECT id FROM identity.account WHERE id=:id FOR UPDATE"),
                    {"id": account_id},
                )
            barrier.wait(timeout=5)
            return login_totp_step(
                db,
                challenge_token=challenges[index].challenge_token,
                code=codes[index],
                dependencies=deps,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=10)
            for future in [executor.submit(consume, index) for index in range(2)]
        ]
    assert all(isinstance(value, IssuedSession) for value in outcomes)
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM identity.session WHERE kind='FULL' AND revoked_at IS NULL"
                )
            ).scalar_one()
            == 1
        )
    with identity_owner_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='identity.sessions.revoked' AND reason='session cap'"
                )
            ).scalar_one()
            >= 2
        )


def test_expired_password_enters_restricted_setup_without_rewriting_password_fact(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        repository_factory=dependencies().repository_factory,
        secret_manager=StaticSecrets(),
        policy=ExpiringPasswordPolicy(),
        clock=clock,
        random=SystemRandom(),
        audit=dependencies().audit,
        on_auth_change=dependencies().on_auth_change,
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
    assert isinstance(result, IssuedSession)
    assert result.kind is SessionKind.BOOTSTRAP
    assert result.bootstrap_purpose is BootstrapPurpose.PASSWORD_EXPIRED
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(text("SELECT password_set_at FROM identity.account")).scalar_one()
            == original_set_at
        )
        assert db.execute(text("SELECT count(*) FROM identity.auth_challenge")).scalar_one() == 0


def test_password_expired_bootstrap_cannot_replace_or_confirm_totp(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        repository_factory=dependencies().repository_factory,
        secret_manager=StaticSecrets(),
        policy=ExpiringPasswordPolicy(),
        clock=clock,
        random=SystemRandom(),
        audit=dependencies().audit,
        on_auth_change=dependencies().on_auth_change,
    )
    secret, _ = _initialize_account(identity_rw_engine, deps, monkeypatch)
    clock.value += timedelta(days=90)
    with identity_rw_engine.begin() as db:
        restricted = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-a",
            dependencies=deps,
        )
    assert isinstance(restricted, IssuedSession)
    assert restricted.bootstrap_purpose is BootstrapPurpose.PASSWORD_EXPIRED

    with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
        enroll_totp(
            db,
            bootstrap_token=restricted.raw_token,
            dependencies=deps,
        )
    code = pyotp.TOTP(secret).at(clock.value)
    with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
        confirm_totp(
            db,
            bootstrap_token=restricted.raw_token,
            code=code,
            dependencies=deps,
        )


def test_password_expired_setup_revokes_bootstrap_and_requires_normal_login(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        repository_factory=dependencies().repository_factory,
        secret_manager=StaticSecrets(),
        policy=ExpiringPasswordPolicy(),
        clock=clock,
        random=SystemRandom(),
        audit=dependencies().audit,
        on_auth_change=dependencies().on_auth_change,
    )
    _initialize_account(identity_rw_engine, deps, monkeypatch)
    clock.value += timedelta(days=90)
    with identity_rw_engine.begin() as db:
        restricted = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-a",
            dependencies=deps,
        )
    assert isinstance(restricted, IssuedSession)

    replacement = "N3w!Secure#Password2026"
    with identity_rw_engine.begin() as db:
        complete_password_setup(
            db,
            bootstrap_token=restricted.raw_token,
            password=replacement,
            dependencies=deps,
        )
    with identity_rw_engine.connect() as db:
        active = db.execute(
            text("SELECT kind, bootstrap_purpose FROM identity.session WHERE revoked_at IS NULL")
        ).all()
    assert active == []
    with identity_rw_engine.begin() as db:
        result = login_password_step(
            db,
            employee_no="00000001",
            password=replacement,
            source="source-a",
            dependencies=deps,
        )
    assert isinstance(result, LoginChallenge)


def test_password_reset_bootstrap_preserves_and_requires_the_existing_totp(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = dependencies(clock=clock)
    secret, _ = _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.connect() as db:
        before = db.execute(
            text("SELECT id, totp_sealed, totp_confirmed_at FROM identity.account")
        ).one()
    with identity_rw_engine.begin() as db:
        temporary_password = issue_temp_password(
            db,
            account_id=str(before.id),
            actor=SYSTEM,
            reason="reset",
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db:
        reset = consume_temp_password(
            db,
            employee_no="00000001",
            temp_password=temporary_password,
            dependencies=deps,
        )
    assert reset is not None
    assert reset.bootstrap_purpose is BootstrapPurpose.PASSWORD_RESET

    with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
        enroll_totp(db, bootstrap_token=reset.raw_token, dependencies=deps)
    clock.value += timedelta(seconds=30)
    with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
        confirm_totp(
            db,
            bootstrap_token=reset.raw_token,
            code=pyotp.TOTP(secret).at(clock.value),
            dependencies=deps,
        )
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM identity.session WHERE kind='FULL' AND revoked_at IS NULL"
                )
            ).scalar_one()
            == 0
        )

    replacement = "N3w!Secure#Password2026"
    with identity_rw_engine.begin() as db:
        complete_password_setup(
            db,
            bootstrap_token=reset.raw_token,
            password=replacement,
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db, pytest.raises(AuthenticationFailed):
        enroll_totp(db, bootstrap_token=reset.raw_token, dependencies=deps)
    with identity_rw_engine.begin() as db:
        full = confirm_totp(
            db,
            bootstrap_token=reset.raw_token,
            code=pyotp.TOTP(secret).at(clock.value),
            dependencies=deps,
        )
    assert full.kind is SessionKind.FULL
    with identity_rw_engine.connect() as db:
        after = db.execute(
            text("SELECT totp_sealed, totp_confirmed_at FROM identity.account")
        ).one()
    assert after.totp_sealed == before.totp_sealed
    assert after.totp_confirmed_at == before.totp_confirmed_at


def test_login_backoff_is_server_side_exponential_and_source_isolated(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = dependencies(clock=clock)
    _initialize_account(identity_rw_engine, deps, monkeypatch)
    for _ in range(5):
        with identity_rw_engine.begin() as db:
            denied = login_password_step(
                db,
                employee_no="00000001",
                password="wrong",
                source="source-a",
                dependencies=deps,
            )
        assert isinstance(denied, AuthenticationDenial)
        assert denied.code is AuthDenialCode.INVALID_CREDENTIALS
    with identity_rw_engine.begin() as db:
        blocked = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-a",
            dependencies=deps,
        )
    assert isinstance(blocked, AuthenticationDenial)
    assert blocked.code is AuthDenialCode.BACKOFF_ACTIVE
    assert blocked.retry_after_seconds == 30

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

    with identity_rw_engine.begin() as db:
        first_after_reset_window = login_password_step(
            db,
            employee_no="00000001",
            password="wrong",
            source="source-c",
            dependencies=deps,
        )
    assert isinstance(first_after_reset_window, AuthenticationDenial)
    clock.value += timedelta(hours=24)
    with identity_rw_engine.begin() as db:
        reset_failure = login_password_step(
            db,
            employee_no="00000001",
            password="wrong",
            source="source-c",
            dependencies=deps,
        )
    assert isinstance(reset_failure, AuthenticationDenial)
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
        with identity_rw_engine.begin() as db:
            denied = login_password_step(
                db,
                employee_no="00000001",
                password="wrong",
                source="source-a",
                dependencies=deps,
            )
        assert isinstance(denied, AuthenticationDenial)
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
        repository_factory=dependencies().repository_factory,
        secret_manager=StaticSecrets(),
        policy=TightAuthPolicy(),
        clock=clock,
        random=SystemRandom(),
        audit=dependencies().audit,
        on_auth_change=dependencies().on_auth_change,
    )
    secret, _ = _initialize_account(identity_rw_engine, deps, monkeypatch)
    for _ in range(2):
        with identity_rw_engine.begin() as db:
            denied = login_password_step(
                db,
                employee_no="00000001",
                password="wrong",
                source="source-a",
                dependencies=deps,
            )
        assert isinstance(denied, AuthenticationDenial)
    with identity_rw_engine.begin() as db:
        blocked = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-a",
            dependencies=deps,
        )
    assert isinstance(blocked, AuthenticationDenial)
    assert blocked.code is AuthDenialCode.BACKOFF_ACTIVE
    assert blocked.retry_after_seconds == 30

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
        with identity_rw_engine.begin() as db:
            denied = login_totp_step(
                db,
                challenge_token=challenge.challenge_token,
                code=invalid_code,
                dependencies=deps,
            )
        assert isinstance(denied, AuthenticationDenial)
    with identity_rw_engine.connect() as db:
        state = db.execute(
            text("SELECT attempt_count, revoked_at FROM identity.auth_challenge")
        ).one()
    assert state.attempt_count == 2
    assert state.revoked_at is not None


def test_two_connections_keep_both_first_backoff_failures(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    with identity_owner_engine.begin() as db:
        db.execute(
            text(
                "CREATE FUNCTION identity.test_pause_backoff() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(0.2); RETURN NEW; END $$"
            )
        )
        db.execute(
            text(
                "CREATE TRIGGER test_pause_backoff BEFORE INSERT OR UPDATE "
                "ON identity.login_backoff FOR EACH ROW "
                "EXECUTE FUNCTION identity.test_pause_backoff()"
            )
        )
    barrier = Barrier(2)

    def fail() -> None:
        with identity_rw_engine.begin() as db:
            barrier.wait(timeout=5)
            denied = login_password_step(
                db,
                employee_no="99999999",
                password="wrong",
                source="source-a",
                dependencies=dependencies(),
            )
            assert isinstance(denied, AuthenticationDenial)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            [future.result(timeout=10) for future in [executor.submit(fail) for _ in range(2)]]
        with identity_rw_engine.connect() as db:
            count = db.execute(
                text(
                    "SELECT failure_count FROM identity.login_backoff "
                    "WHERE employee_no='99999999' AND source='source-a'"
                )
            ).scalar_one()
        assert count == 2
    finally:
        with identity_owner_engine.begin() as db:
            db.execute(text("DROP TRIGGER test_pause_backoff ON identity.login_backoff"))
            db.execute(text("DROP FUNCTION identity.test_pause_backoff()"))


def test_password_denial_commits_backoff_and_audit_without_exception_control_flow(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = dependencies()
    _initialize_account(identity_rw_engine, deps, monkeypatch)

    with identity_rw_engine.begin() as db:
        result = login_password_step(
            db,
            employee_no="00000001",
            password="wrong",
            source="source-a",
            dependencies=deps,
        )

    assert getattr(result, "code", None) == "INVALID_CREDENTIALS"
    assert getattr(result, "retry_after_seconds", None) is None
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT failure_count FROM identity.login_backoff "
                    "WHERE employee_no='00000001' AND source='source-a'"
                )
            ).scalar_one()
            == 1
        )
    with identity_owner_engine.connect() as db:
        denied = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='identity.login.password' AND result='DENIED'"
            )
        ).scalar_one()
    assert denied == 1


def test_totp_denial_commits_attempt_and_audit_without_exception_control_flow(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = dependencies()
    _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.begin() as db:
        challenge = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-a",
            dependencies=deps,
        )
    assert isinstance(challenge, LoginChallenge)

    with identity_rw_engine.begin() as db:
        result = login_totp_step(
            db,
            challenge_token=challenge.challenge_token,
            code="000000",
            dependencies=deps,
        )

    assert getattr(result, "code", None) == "INVALID_CHALLENGE"
    with identity_rw_engine.connect() as db:
        assert (
            db.execute(text("SELECT attempt_count FROM identity.auth_challenge")).scalar_one() == 1
        )
    with identity_owner_engine.connect() as db:
        denied = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='identity.login.totp' AND result='DENIED'"
            )
        ).scalar_one()
    assert denied == 1


def test_active_backoff_is_a_typed_denial_with_retry_after(
    clean_identity_db: None,
    identity_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    deps = IdentityDependencies(
        repository_factory=dependencies().repository_factory,
        secret_manager=StaticSecrets(),
        policy=TightAuthPolicy(),
        clock=clock,
        random=SystemRandom(),
        audit=dependencies().audit,
        on_auth_change=dependencies().on_auth_change,
    )
    _initialize_account(identity_rw_engine, deps, monkeypatch)
    with identity_rw_engine.begin() as db:
        first = login_password_step(
            db,
            employee_no="00000001",
            password="wrong",
            source="source-a",
            dependencies=deps,
        )
    with identity_rw_engine.begin() as db:
        second = login_password_step(
            db,
            employee_no="00000001",
            password="wrong",
            source="source-a",
            dependencies=deps,
        )
    assert getattr(first, "code", None) == "INVALID_CREDENTIALS"
    assert getattr(second, "code", None) == "INVALID_CREDENTIALS"

    with identity_rw_engine.begin() as db:
        blocked = login_password_step(
            db,
            employee_no="00000001",
            password=VALID_PASSWORD,
            source="source-a",
            dependencies=deps,
        )
    assert getattr(blocked, "code", None) == "BACKOFF_ACTIVE"
    assert getattr(blocked, "retry_after_seconds", None) == 30


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
