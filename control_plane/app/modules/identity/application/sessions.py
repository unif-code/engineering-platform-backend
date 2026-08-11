from datetime import timedelta

from control_plane.app.modules.identity.application.common import audit, token_hash
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.domain.account import AccountStatus
from control_plane.app.modules.identity.domain.models import Principal
from control_plane.app.modules.identity.domain.session import (
    IssuedSession,
    SessionKind,
    SessionPrincipal,
)
from control_plane.app.modules.identity.ports.repository import IdentityRepository


def issue_session(
    repository: IdentityRepository,
    *,
    account_id: str,
    kind: SessionKind,
    dependencies: IdentityDependencies,
) -> IssuedSession:
    now = dependencies.clock.now()
    raw_token = dependencies.random.token_urlsafe(32)
    repository.insert_session(
        id=str(dependencies.random.uuid4()),
        account_id=account_id,
        token_hash=token_hash(raw_token),
        kind=kind.value,
        now=now,
        expires_hint=now + timedelta(days=365),
    )
    return IssuedSession(account_id=account_id, raw_token=raw_token, kind=kind)


def validate_session(
    repository: IdentityRepository,
    *,
    raw_token: str,
    dependencies: IdentityDependencies,
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
        repository.revoke_session(str(row["session_id"]), now, "IDLE_TIMEOUT")
        deps.on_auth_change(str(row["account_id"]))
        return None
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
        is_super_admin=row["is_super_admin"],
    )


def logout(
    repository: IdentityRepository,
    *,
    raw_token: str,
    dependencies: IdentityDependencies,
) -> bool:
    deps = dependencies
    db = repository.db
    row = repository.session_with_account(token_hash(raw_token), for_update=True)
    if row is None:
        return False
    account_id = str(row["account_id"])
    changed = repository.revoke_session(str(row["session_id"]), deps.clock.now(), "LOGOUT")
    if changed:
        audit(
            db,
            actor=Principal(employee_id=row["employee_no"], name=row["display_name"]),
            action="identity.session.logout",
            target_type="account",
            target_id=account_id,
            result="SUCCESS",
            reason="logout",
        )
        deps.on_auth_change(account_id)
    return changed


def revoke_sessions_for(
    repository: IdentityRepository,
    *,
    account_id: str,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies,
) -> int:
    deps = dependencies
    db = repository.db
    now = deps.clock.now()
    count = repository.revoke_sessions(account_id, now, reason)
    audit(
        db,
        actor=actor,
        action="identity.sessions.revoked",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason=reason,
    )
    deps.on_auth_change(account_id)
    return count
