from datetime import datetime
from typing import Any

from control_plane.app.modules.identity.application.common import audit
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.security_change import (
    notify_identity_change,
)
from control_plane.app.modules.identity.application.sessions import (
    finalize_session_revocations,
    issue_session,
)
from control_plane.app.modules.identity.domain.account import (
    AccountDto,
    AccountStatus,
    OrganizationAccountDto,
    ensure_account_transition_allowed,
    ensure_effective_super_admin_remains,
)
from control_plane.app.modules.identity.domain.errors import (
    AccountNotFound,
    StaleAccountVersion,
)
from control_plane.app.modules.identity.domain.models import Principal
from control_plane.app.modules.identity.domain.session import (
    BootstrapPurpose,
    IssuedSession,
    SessionKind,
)
from control_plane.app.modules.identity.ports.repository import IdentityRepository
from control_plane.app.shared.security import hash_password, verify_password


def _dto(row: Any) -> AccountDto:
    return AccountDto(
        id=str(row["id"]),
        employee_no=row["employee_no"],
        display_name=row["display_name"],
        profession=row["profession"],
        status=AccountStatus(row["status"]),
        password_set_at=row["password_set_at"],
        totp_confirmed_at=row["totp_confirmed_at"],
        is_super_admin=row["is_super_admin"],
        version=row["version"],
    )


def get_organization_account(
    repository: IdentityRepository,
    *,
    account_id: str,
) -> OrganizationAccountDto | None:
    row = repository.account_by_id(account_id)
    if row is None:
        return None
    return OrganizationAccountDto(
        id=str(row["id"]),
        employee_no=row["employee_no"],
        display_name=row["display_name"],
        status=AccountStatus(row["status"]),
        initialized=row["password_hash"] is not None and row["totp_confirmed_at"] is not None,
    )


def _issue_temp(
    repository: IdentityRepository,
    *,
    account_id: str,
    actor: Principal,
    now: datetime,
    dependencies: IdentityDependencies,
    expires_at: datetime | None = None,
) -> str:
    policy = dependencies.policy.get_identity_policy(repository.db)
    material = dependencies.secret_manager.load()
    temporary_password = dependencies.random.token_urlsafe(24)
    issuer = repository.account_by_employee_no(actor.employee_id)
    issued_by = str(issuer["id"]) if issuer is not None else account_id
    repository.invalidate_temp_credentials(account_id, now)
    repository.insert_temp_credential(
        id=str(dependencies.random.uuid4()),
        account_id=account_id,
        secret_hash=hash_password(temporary_password, pepper=material.password_pepper),
        expires_at=expires_at or now + policy.temp_credential_ttl,
        issued_by=issued_by,
        created_at=now,
    )
    return temporary_password


def create_account(
    repository: IdentityRepository,
    *,
    employee_no: str,
    display_name: str,
    actor: Principal,
    reason: str,
    profession: str | None = None,
    dependencies: IdentityDependencies,
    correlation_id: str | None = None,
) -> tuple[AccountDto, str]:
    deps = dependencies
    db = repository.db
    if len(employee_no) != 8 or not employee_no.isascii() or not employee_no.isdigit():
        raise ValueError("employee number must be an eight-digit string")
    account_id = str(deps.random.uuid4())
    now = deps.clock.now()
    repository.insert_account(
        id=account_id,
        employee_no=employee_no,
        display_name=display_name,
        profession=profession,
        status=AccountStatus.PENDING_INIT.value,
        now=now,
    )
    temporary_password = _issue_temp(
        repository,
        account_id=account_id,
        actor=actor,
        now=now,
        dependencies=deps,
    )
    audit(
        db,
        dependencies=deps,
        actor=actor,
        action="identity.account.created",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason=reason,
        correlation_id=correlation_id,
    )
    audit(
        db,
        dependencies=deps,
        actor=actor,
        action="identity.temp_credential.issued",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason=reason,
        correlation_id=correlation_id,
    )
    row = repository.account_by_id(account_id)
    assert row is not None
    return _dto(row), temporary_password


def issue_temp_password(
    repository: IdentityRepository,
    *,
    account_id: str,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies,
) -> str:
    deps = dependencies
    db = repository.db
    repository.lock_super_admin_invariant()
    row = repository.account_by_id(account_id, for_update=True)
    if row is None:
        raise AccountNotFound(account_id)
    ensure_effective_super_admin_remains(
        is_super_admin=row["is_super_admin"],
        status=AccountStatus(row["status"]),
        password_initialized=row["password_hash"] is not None,
        totp_initialized=row["totp_confirmed_at"] is not None,
        other_effective_super_admins=repository.effective_super_admins_except(account_id),
    )
    now = deps.clock.now()
    temporary_password = _issue_temp(
        repository,
        account_id=account_id,
        actor=actor,
        now=now,
        dependencies=deps,
    )
    repository.reset_password_state(account_id, now)
    revoked = repository.revoke_sessions(account_id, now, "PASSWORD_RESET")
    audit(
        db,
        dependencies=deps,
        actor=actor,
        action="identity.temp_credential.issued",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason=reason,
    )
    audit(
        db,
        dependencies=deps,
        actor=actor,
        action="identity.password.reset",
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
        reason="password reset",
        dependencies=deps,
        invoke_hook=False,
    )
    notify_identity_change(deps.on_auth_change, account_id)
    return temporary_password


def consume_temp_password(
    repository: IdentityRepository,
    *,
    employee_no: str,
    temp_password: str,
    dependencies: IdentityDependencies,
) -> IssuedSession | None:
    deps = dependencies
    db = repository.db
    now = deps.clock.now()
    credential = repository.lock_active_temp_credential(employee_no, now)
    if credential is None:
        return None
    material = deps.secret_manager.load()
    if not verify_password(
        temp_password,
        credential["secret_hash"],
        pepper=material.password_pepper,
    ):
        return None
    if not repository.consume_temp_credential(str(credential["id"]), now):
        return None
    issued = issue_session(
        repository,
        account_id=str(credential["account_id"]),
        kind=SessionKind.BOOTSTRAP,
        bootstrap_purpose=(
            BootstrapPurpose.PASSWORD_RESET
            if credential["totp_confirmed_at"] is not None
            else BootstrapPurpose.INITIAL_SETUP
        ),
        dependencies=deps,
    )
    audit(
        db,
        dependencies=deps,
        actor=Principal(employee_id=employee_no, name=employee_no),
        action="identity.temp_credential.consumed",
        target_type="account",
        target_id=str(credential["account_id"]),
        result="SUCCESS",
        reason="bootstrap",
    )
    return issued


def set_account_status(
    repository: IdentityRepository,
    *,
    account_id: str,
    status: AccountStatus,
    expected_version: int,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies,
) -> AccountDto:
    deps = dependencies
    db = repository.db
    repository.lock_super_admin_invariant()
    account = repository.account_by_id(account_id, for_update=True)
    if account is None:
        raise AccountNotFound(account_id)
    if account["version"] != expected_version:
        raise StaleAccountVersion(account_id)
    current = AccountStatus(account["status"])
    ensure_account_transition_allowed(
        current=current,
        target=status,
        password_initialized=account["password_hash"] is not None,
        totp_initialized=account["totp_confirmed_at"] is not None,
    )
    if status is not AccountStatus.ENABLED:
        ensure_effective_super_admin_remains(
            is_super_admin=account["is_super_admin"],
            status=current,
            password_initialized=account["password_hash"] is not None,
            totp_initialized=account["totp_confirmed_at"] is not None,
            other_effective_super_admins=repository.effective_super_admins_except(account_id),
        )
    now = deps.clock.now()
    updated = repository.update_account_status(account_id, status.value, expected_version, now)
    if updated is None:
        raise StaleAccountVersion(account_id)
    if status is not AccountStatus.ENABLED:
        revoked = repository.revoke_sessions(account_id, now, f"ACCOUNT_{status.value}")
        finalize_session_revocations(
            repository,
            account_id=account_id,
            revoked_session_ids=revoked,
            actor=actor,
            reason=f"account {status.value.lower()}",
            dependencies=deps,
            invoke_hook=False,
        )
    audit(
        db,
        dependencies=deps,
        actor=actor,
        action="identity.account.status.changed",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason=reason,
    )
    notify_identity_change(deps.on_auth_change, account_id)
    return _dto(updated)
