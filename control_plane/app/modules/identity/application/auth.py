from datetime import timedelta
from typing import Any

from control_plane.app.modules.identity.application.accounts import consume_temp_password
from control_plane.app.modules.identity.application.common import audit, token_hash
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.sessions import (
    finalize_session_revocations,
    issue_session,
)
from control_plane.app.modules.identity.domain.account import AccountStatus
from control_plane.app.modules.identity.domain.errors import (
    AuthenticationFailed,
    PasswordFloorViolation,
    TotpChallengeFailed,
)
from control_plane.app.modules.identity.domain.models import Principal
from control_plane.app.modules.identity.domain.session import (
    AuthChallengeState,
    AuthDenialCode,
    AuthenticationDenial,
    BootstrapPurpose,
    IssuedSession,
    LoginChallenge,
    SessionKind,
    TotpEnrollment,
)
from control_plane.app.modules.identity.ports.repository import IdentityRepository
from control_plane.app.shared.security import (
    hash_password,
    seal,
    totp_provisioning_uri,
    unseal,
    validate_password_floor,
    verify_password,
    verify_totp,
)

_CHALLENGE_TTL = timedelta(minutes=5)


def _challenge_denial(
    *,
    attempt_count: int = 0,
    attempt_limit: int = 0,
) -> AuthenticationDenial:
    retry_allowed = attempt_count > 0 and attempt_count < attempt_limit
    return AuthenticationDenial(
        code=AuthDenialCode.INVALID_CHALLENGE,
        challenge_state=(
            AuthChallengeState.RETRY_ALLOWED if retry_allowed else AuthChallengeState.TERMINAL
        ),
    )


def _bootstrap_purpose(row: Any) -> BootstrapPurpose:
    try:
        return BootstrapPurpose(row["bootstrap_purpose"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationFailed("valid bootstrap session required") from exc


def _bootstrap_account(
    repository: IdentityRepository,
    raw_token: str,
    dependencies: IdentityDependencies,
) -> Any:
    row = repository.session_with_account(token_hash(raw_token), for_update=True)
    if row is None or row["revoked_at"] is not None or row["kind"] != SessionKind.BOOTSTRAP.value:
        raise AuthenticationFailed("valid bootstrap session required")
    if row["status"] not in {
        AccountStatus.PENDING_INIT.value,
        AccountStatus.ENABLED.value,
    }:
        raise AuthenticationFailed("valid bootstrap session required")
    now = dependencies.clock.now()
    policy = dependencies.policy.get_identity_policy(repository.db)
    if now >= row["last_seen_at"] + policy.session_idle_timeout:
        revoked = repository.revoke_session(str(row["session_id"]), now, "IDLE_TIMEOUT")
        finalize_session_revocations(
            repository,
            account_id=str(row["account_id"]),
            revoked_session_ids=[revoked] if revoked is not None else [],
            actor=Principal(employee_id=row["employee_no"], name=row["display_name"]),
            reason="idle timeout",
            dependencies=dependencies,
        )
        raise AuthenticationFailed("valid bootstrap session required")
    repository.touch_session(
        str(row["session_id"]),
        now,
        now + policy.session_idle_timeout,
    )
    return row


def complete_password_setup(
    repository: IdentityRepository,
    *,
    bootstrap_token: str,
    password: str,
    dependencies: IdentityDependencies,
) -> None:
    deps = dependencies
    db = repository.db
    row = _bootstrap_account(repository, bootstrap_token, deps)
    purpose = _bootstrap_purpose(row)
    violations = validate_password_floor(
        password,
        context=[row["employee_no"], row["display_name"]],
    )
    if violations:
        raise PasswordFloorViolation(violations)
    now = deps.clock.now()
    account_id = str(row["account_id"])
    repository.update_password(
        account_id,
        hash_password(password, pepper=deps.secret_manager.load().password_pepper),
        now,
    )
    revoked = repository.revoke_sessions(
        account_id,
        now,
        "PASSWORD_CHANGED",
        except_session_id=(
            None if purpose is BootstrapPurpose.PASSWORD_EXPIRED else str(row["session_id"])
        ),
    )
    finalize_session_revocations(
        repository,
        account_id=account_id,
        revoked_session_ids=revoked,
        actor=Principal(employee_id=row["employee_no"], name=row["display_name"]),
        reason="password changed",
        dependencies=deps,
        invoke_hook=False,
    )
    audit(
        db,
        dependencies=deps,
        actor=Principal(employee_id=row["employee_no"], name=row["display_name"]),
        action="identity.password.setup.completed",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason="bootstrap",
    )
    deps.on_auth_change(account_id)


def enroll_totp(
    repository: IdentityRepository,
    *,
    bootstrap_token: str,
    dependencies: IdentityDependencies,
) -> TotpEnrollment:
    deps = dependencies
    db = repository.db
    row = _bootstrap_account(repository, bootstrap_token, deps)
    if _bootstrap_purpose(row) is not BootstrapPurpose.INITIAL_SETUP:
        raise AuthenticationFailed("TOTP enrollment is not allowed for this bootstrap session")
    if row["password_hash"] is None:
        raise AuthenticationFailed("password setup required")
    secret = deps.random.totp_secret()
    account_id = str(row["account_id"])
    repository.update_totp_enrollment(
        account_id,
        seal(secret.encode("ascii"), deps.secret_manager.load().totp_sealing_key),
        deps.clock.now(),
    )
    audit(
        db,
        dependencies=deps,
        actor=Principal(employee_id=row["employee_no"], name=row["display_name"]),
        action="identity.totp.enrolled",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason="bootstrap",
    )
    return TotpEnrollment(
        secret=secret,
        provisioning_uri=totp_provisioning_uri(secret, row["employee_no"]),
    )


def confirm_totp(
    repository: IdentityRepository,
    *,
    bootstrap_token: str,
    code: str,
    dependencies: IdentityDependencies,
) -> IssuedSession:
    deps = dependencies
    db = repository.db
    row = _bootstrap_account(repository, bootstrap_token, deps)
    purpose = _bootstrap_purpose(row)
    if purpose is BootstrapPurpose.PASSWORD_EXPIRED:
        raise AuthenticationFailed("TOTP confirmation is not allowed for this bootstrap session")
    if row["password_hash"] is None or row["totp_sealed"] is None:
        raise AuthenticationFailed("password and TOTP enrollment required")
    material = deps.secret_manager.load()
    secret = unseal(row["totp_sealed"], material.totp_sealing_key).decode("ascii")
    step = verify_totp(secret, code, last_used_step=row["totp_last_step"])
    if step is None:
        raise TotpChallengeFailed("invalid or replayed TOTP")
    now = deps.clock.now()
    account_id = str(row["account_id"])
    if purpose is BootstrapPurpose.INITIAL_SETUP:
        repository.confirm_totp(account_id, step, now)
    else:
        if row["totp_confirmed_at"] is None:
            raise AuthenticationFailed("confirmed TOTP required for password reset")
        repository.restore_password_reset(account_id, step, now)
    revoked = repository.revoke_sessions(account_id, now, "BOOTSTRAP_COMPLETED")
    actor = Principal(employee_id=row["employee_no"], name=row["display_name"])
    finalize_session_revocations(
        repository,
        account_id=account_id,
        revoked_session_ids=revoked,
        actor=actor,
        reason="bootstrap completed",
        dependencies=deps,
        invoke_hook=False,
    )
    issued = issue_session(
        repository,
        account_id=account_id,
        kind=SessionKind.FULL,
        dependencies=deps,
    )
    policy = deps.policy.get_identity_policy(db)
    evicted = repository.evict_old_full_sessions(account_id, policy.session_cap, now)
    finalize_session_revocations(
        repository,
        account_id=account_id,
        revoked_session_ids=evicted,
        actor=actor,
        reason="session cap",
        dependencies=deps,
    )
    audit(
        db,
        dependencies=deps,
        actor=actor,
        action="identity.totp.confirmed",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason="bootstrap",
    )
    deps.on_auth_change(account_id)
    return issued


def _check_backoff(
    repository: IdentityRepository,
    employee_no: str,
    source: str,
    dependencies: IdentityDependencies,
) -> AuthenticationDenial | None:
    if not source.strip():
        raise ValueError("login source must not be empty")
    repository.lock_backoff_scope(employee_no, source)
    now = dependencies.clock.now()
    policy = dependencies.policy.get_identity_policy(repository.db)
    row = repository.backoff_by_scope(employee_no, source, for_update=True)
    if row is None:
        return None
    if (
        row["last_failure_at"] is not None
        and now - row["last_failure_at"] >= policy.backoff_reset_after
    ):
        repository.save_backoff(employee_no, source, 0, None, None)
        return None
    if row["locked_until"] is not None and now < row["locked_until"]:
        retry = max(1, int((row["locked_until"] - now).total_seconds()))
        return AuthenticationDenial(
            code=AuthDenialCode.BACKOFF_ACTIVE,
            retry_after_seconds=retry,
        )
    return None


def _record_password_failure(
    repository: IdentityRepository,
    employee_no: str,
    source: str,
    dependencies: IdentityDependencies,
) -> None:
    now = dependencies.clock.now()
    policy = dependencies.policy.get_identity_policy(repository.db)
    row = repository.backoff_by_scope(employee_no, source, for_update=True)
    count = 1
    if row is not None and row["last_failure_at"] is not None:
        if now - row["last_failure_at"] < policy.backoff_reset_after:
            count = row["failure_count"] + 1
    locked_until = None
    if count >= policy.backoff_threshold:
        exponent = count - policy.backoff_threshold
        seconds = min(
            policy.backoff_initial_delay.total_seconds() * (2**exponent),
            policy.backoff_max_delay.total_seconds(),
        )
        locked_until = now + timedelta(seconds=seconds)
    repository.save_backoff(employee_no, source, count, now, locked_until)


def login_password_step(
    repository: IdentityRepository,
    *,
    employee_no: str,
    password: str,
    source: str,
    dependencies: IdentityDependencies,
) -> LoginChallenge | IssuedSession | AuthenticationDenial:
    deps = dependencies
    db = repository.db
    backoff_denial = _check_backoff(repository, employee_no, source, deps)
    if backoff_denial is not None:
        audit(
            db,
            dependencies=deps,
            actor=Principal(employee_id=employee_no, name=employee_no),
            action="identity.login.password",
            target_type="account",
            target_id=employee_no,
            result="DENIED",
            reason="backoff active",
        )
        return backoff_denial
    account = repository.account_by_employee_no(employee_no, for_update=True)
    if account is not None and account["status"] == AccountStatus.PENDING_INIT.value:
        issued = consume_temp_password(
            repository,
            employee_no=employee_no,
            temp_password=password,
            dependencies=deps,
        )
        if issued is not None:
            repository.save_backoff(employee_no, source, 0, None, None)
            return issued
    material = deps.secret_manager.load()
    valid = (
        account is not None
        and account["status"] == AccountStatus.ENABLED.value
        and account["password_hash"] is not None
        and verify_password(password, account["password_hash"], pepper=material.password_pepper)
    )
    if not valid:
        _record_password_failure(repository, employee_no, source, deps)
        audit(
            db,
            dependencies=deps,
            actor=Principal(employee_id=employee_no, name=employee_no),
            action="identity.login.password",
            target_type="account",
            target_id=employee_no,
            result="DENIED",
            reason="invalid credentials",
        )
        return AuthenticationDenial(code=AuthDenialCode.INVALID_CREDENTIALS)
    assert account is not None
    repository.save_backoff(employee_no, source, 0, None, None)
    policy = deps.policy.get_identity_policy(db)
    now = deps.clock.now()
    if (
        policy.password_max_age is not None
        and account["password_set_at"] is not None
        and now >= account["password_set_at"] + policy.password_max_age
    ):
        return issue_session(
            repository,
            account_id=str(account["id"]),
            kind=SessionKind.BOOTSTRAP,
            bootstrap_purpose=BootstrapPurpose.PASSWORD_EXPIRED,
            dependencies=deps,
        )
    raw_token = deps.random.token_urlsafe(32)
    repository.insert_challenge(
        id=str(deps.random.uuid4()),
        token_hash=token_hash(raw_token),
        purpose="LOGIN_TOTP",
        account_id=str(account["id"]),
        issued_at=now,
        expires_at=now + _CHALLENGE_TTL,
        attempt_limit=policy.totp_attempt_cap,
    )
    audit(
        db,
        dependencies=deps,
        actor=Principal(employee_id=employee_no, name=account["display_name"]),
        action="identity.login.password",
        target_type="account",
        target_id=str(account["id"]),
        result="SUCCESS",
        reason="TOTP challenge issued",
    )
    return LoginChallenge(account_id=str(account["id"]), challenge_token=raw_token)


def login_totp_step(
    repository: IdentityRepository,
    *,
    challenge_token: str,
    code: str,
    dependencies: IdentityDependencies,
) -> IssuedSession | AuthenticationDenial:
    deps = dependencies
    db = repository.db
    now = deps.clock.now()
    challenge = repository.challenge_by_hash(token_hash(challenge_token), for_update=True)
    if challenge is None:
        audit(
            db,
            dependencies=deps,
            actor=Principal(employee_id="SYSTEM", name="System"),
            action="identity.login.totp",
            target_type="account",
            target_id="unknown",
            result="DENIED",
            reason="invalid challenge or TOTP",
        )
        return _challenge_denial()
    active = (
        challenge["purpose"] == "LOGIN_TOTP"
        and challenge["status"] == AccountStatus.ENABLED.value
        and challenge["consumed_at"] is None
        and challenge["revoked_at"] is None
        and now < challenge["expires_at"]
        and challenge["attempt_count"] < challenge["attempt_limit"]
        and challenge["totp_sealed"] is not None
    )
    step = None
    if active:
        material = deps.secret_manager.load()
        secret = unseal(challenge["totp_sealed"], material.totp_sealing_key).decode("ascii")
        step = verify_totp(secret, code, last_used_step=challenge["totp_last_step"])
    if step is None:
        attempt_count = repository.fail_challenge(str(challenge["challenge_id"]), now)
        audit(
            db,
            dependencies=deps,
            actor=Principal(employee_id=challenge["employee_no"], name=challenge["display_name"]),
            action="identity.login.totp",
            target_type="account",
            target_id=str(challenge["account_id"]),
            result="DENIED",
            reason="invalid challenge or TOTP",
        )
        return _challenge_denial(
            attempt_count=attempt_count,
            attempt_limit=challenge["attempt_limit"],
        )
    account_id = str(challenge["account_id"])
    if not repository.update_totp_step(account_id, step, now):
        attempt_count = repository.fail_challenge(str(challenge["challenge_id"]), now)
        audit(
            db,
            dependencies=deps,
            actor=Principal(employee_id=challenge["employee_no"], name=challenge["display_name"]),
            action="identity.login.totp",
            target_type="account",
            target_id=account_id,
            result="DENIED",
            reason="invalid challenge or TOTP",
        )
        return _challenge_denial(
            attempt_count=attempt_count,
            attempt_limit=challenge["attempt_limit"],
        )
    if not repository.consume_challenge(str(challenge["challenge_id"]), now):
        return _challenge_denial()
    issued = issue_session(
        repository,
        account_id=account_id,
        kind=SessionKind.FULL,
        dependencies=deps,
    )
    policy = deps.policy.get_identity_policy(db)
    evicted = repository.evict_old_full_sessions(account_id, policy.session_cap, now)
    finalize_session_revocations(
        repository,
        account_id=account_id,
        revoked_session_ids=evicted,
        actor=Principal(employee_id=challenge["employee_no"], name=challenge["display_name"]),
        reason="session cap",
        dependencies=deps,
    )
    audit(
        db,
        dependencies=deps,
        actor=Principal(employee_id=challenge["employee_no"], name=challenge["display_name"]),
        action="identity.login.totp",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason="session issued",
    )
    return issued
