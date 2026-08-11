from datetime import timedelta

from control_plane.app.modules.identity.application.common import audit, token_hash
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.security_change import (
    notify_identity_change,
)
from control_plane.app.modules.identity.domain.account import AccountStatus
from control_plane.app.modules.identity.domain.models import Principal
from control_plane.app.modules.identity.domain.session import (
    BootstrapPurpose,
    IssuedSession,
    SessionKind,
    SessionPrincipal,
)
from control_plane.app.modules.identity.ports.repository import IdentityRepository


def finalize_session_revocations(
    repository: IdentityRepository,
    *,
    account_id: str,
    revoked_session_ids: list[str],
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies,
    invoke_hook: bool = True,
    action: str = "identity.sessions.revoked",
) -> None:
    if not revoked_session_ids:
        return
    audit(
        repository.db,
        dependencies=dependencies,
        actor=actor,
        action=action,
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason=reason,
    )
    if invoke_hook:
        notify_identity_change(dependencies.on_auth_change, account_id)


def issue_session(
    repository: IdentityRepository,
    *,
    account_id: str,
    kind: SessionKind,
    bootstrap_purpose: BootstrapPurpose | None = None,
    dependencies: IdentityDependencies,
) -> IssuedSession:
    now = dependencies.clock.now()
    if kind is SessionKind.FULL:
        repository.lock_account_lifecycle(account_id)
    raw_token = dependencies.random.token_urlsafe(32)
    repository.insert_session(
        id=str(dependencies.random.uuid4()),
        account_id=account_id,
        token_hash=token_hash(raw_token),
        kind=kind.value,
        bootstrap_purpose=bootstrap_purpose.value if bootstrap_purpose is not None else None,
        now=now,
        expires_hint=now + timedelta(days=365),
    )
    return IssuedSession(
        account_id=account_id,
        raw_token=raw_token,
        kind=kind,
        bootstrap_purpose=bootstrap_purpose,
    )


def validate_session(
    repository: IdentityRepository,
    *,
    raw_token: str,
    dependencies: IdentityDependencies,
    touch_activity: bool = True,
) -> SessionPrincipal | None:
    deps = dependencies
    db = repository.db
    row = repository.session_with_account(token_hash(raw_token), for_update=True)
    if row is None or row["revoked_at"] is not None:
        return None
    kind = SessionKind(row["kind"])
    allowed = (
        kind is SessionKind.BOOTSTRAP
        and row["status"] in {AccountStatus.PENDING_INIT.value, AccountStatus.ENABLED.value}
    ) or (
        kind is SessionKind.FULL
        and row["status"] == AccountStatus.ENABLED.value
        and row["password_hash"] is not None
        and row["totp_confirmed_at"] is not None
    )
    if not allowed:
        return None
    now = deps.clock.now()
    policy = deps.policy.get_identity_policy(db)
    if now >= row["last_seen_at"] + policy.session_idle_timeout:
        revoked = repository.revoke_session(str(row["session_id"]), now, "IDLE_TIMEOUT")
        finalize_session_revocations(
            repository,
            account_id=str(row["account_id"]),
            revoked_session_ids=[revoked] if revoked is not None else [],
            actor=Principal(employee_id=row["employee_no"], name=row["display_name"]),
            reason="idle timeout",
            dependencies=deps,
        )
        return None
    if touch_activity:
        repository.touch_session(
            str(row["session_id"]),
            now,
            now + policy.session_idle_timeout,
        )
    return SessionPrincipal(
        account_id=str(row["account_id"]),
        employee_no=row["employee_no"],
        display_name=row["display_name"],
        session_kind=kind,
        bootstrap_purpose=(
            BootstrapPurpose(row["bootstrap_purpose"])
            if row["bootstrap_purpose"] is not None
            else None
        ),
        is_super_admin=row["is_super_admin"],
    )


def logout(
    repository: IdentityRepository,
    *,
    raw_token: str,
    dependencies: IdentityDependencies,
) -> bool:
    deps = dependencies
    row = repository.session_with_account(token_hash(raw_token), for_update=True)
    if row is None:
        return False
    account_id = str(row["account_id"])
    revoked = repository.revoke_session(str(row["session_id"]), deps.clock.now(), "LOGOUT")
    finalize_session_revocations(
        repository,
        account_id=account_id,
        revoked_session_ids=[revoked] if revoked is not None else [],
        actor=Principal(employee_id=row["employee_no"], name=row["display_name"]),
        reason="logout",
        dependencies=deps,
        action="identity.session.logout",
    )
    return revoked is not None


def revoke_sessions_for(
    repository: IdentityRepository,
    *,
    account_id: str,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies,
) -> int:
    deps = dependencies
    now = deps.clock.now()
    revoked = repository.revoke_sessions(account_id, now, reason)
    audit(
        repository.db,
        dependencies=deps,
        actor=actor,
        action="identity.sessions.revoke.requested",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason=reason,
    )
    finalize_session_revocations(
        repository,
        account_id=account_id,
        revoked_session_ids=revoked,
        actor=actor,
        reason=reason,
        dependencies=deps,
    )
    return len(revoked)
