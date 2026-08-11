"""Public identity facade; other modules must not import identity internals."""

from sqlalchemy import Connection

from control_plane.app.modules.identity.adapters.policy import DefaultEffectivePolicy
from control_plane.app.modules.identity.adapters.runtime import SystemClock, SystemRandom
from control_plane.app.modules.identity.adapters.sqlalchemy import SqlAlchemyIdentityRepository
from control_plane.app.modules.identity.application.accounts import (
    consume_temp_password as _consume_temp_password,
)
from control_plane.app.modules.identity.application.accounts import (
    create_account as _create_account,
)
from control_plane.app.modules.identity.application.accounts import (
    issue_temp_password as _issue_temp_password,
)
from control_plane.app.modules.identity.application.accounts import (
    set_account_status as _set_account_status,
)
from control_plane.app.modules.identity.application.auth import (
    complete_password_setup as _complete_password_setup,
)
from control_plane.app.modules.identity.application.auth import confirm_totp as _confirm_totp
from control_plane.app.modules.identity.application.auth import enroll_totp as _enroll_totp
from control_plane.app.modules.identity.application.auth import (
    login_password_step as _login_password_step,
)
from control_plane.app.modules.identity.application.auth import (
    login_totp_step as _login_totp_step,
)
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.sessions import logout as _logout
from control_plane.app.modules.identity.application.sessions import (
    revoke_sessions_for as _revoke_sessions_for,
)
from control_plane.app.modules.identity.application.sessions import (
    validate_session as _validate_session,
)
from control_plane.app.modules.identity.domain.account import (
    AccountDto,
    AccountStatus,
    ensure_account_transition_allowed,
    ensure_effective_super_admin_remains,
)
from control_plane.app.modules.identity.domain.errors import (
    AccountConflict,
    AccountNotFound,
    AuthenticationFailed,
    InvalidAccountTransition,
    LastEffectiveSuperAdmin,
    LoginBackoffActive,
    PasswordFloorViolation,
    StaleAccountVersion,
    TotpChallengeFailed,
)
from control_plane.app.modules.identity.domain.models import Principal
from control_plane.app.modules.identity.domain.policy import EffectiveIdentityPolicy
from control_plane.app.modules.identity.domain.session import (
    IssuedSession,
    LoginChallenge,
    SessionKind,
    SessionPrincipal,
    TotpEnrollment,
)
from control_plane.app.modules.identity.ports.policy import EffectivePolicyPort
from control_plane.app.shared.db.settings import SecuritySettings
from control_plane.app.shared.security import FileSecretManager


def _dependencies(dependencies: IdentityDependencies | None) -> IdentityDependencies:
    if dependencies is not None:
        return dependencies
    return IdentityDependencies(
        secret_manager=FileSecretManager(SecuritySettings()),
        policy=DefaultEffectivePolicy(),
        clock=SystemClock(),
        random=SystemRandom(),
    )


def create_account(
    db: Connection,
    *,
    employee_no: str,
    display_name: str,
    actor: Principal,
    reason: str,
    profession: str | None = None,
    dependencies: IdentityDependencies | None = None,
) -> tuple[AccountDto, str]:
    return _create_account(
        SqlAlchemyIdentityRepository(db),
        employee_no=employee_no,
        display_name=display_name,
        actor=actor,
        reason=reason,
        profession=profession,
        dependencies=_dependencies(dependencies),
    )


def issue_temp_password(
    db: Connection,
    *,
    account_id: str,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies | None = None,
) -> str:
    return _issue_temp_password(
        SqlAlchemyIdentityRepository(db),
        account_id=account_id,
        actor=actor,
        reason=reason,
        dependencies=_dependencies(dependencies),
    )


def consume_temp_password(
    db: Connection,
    *,
    employee_no: str,
    temp_password: str,
    dependencies: IdentityDependencies | None = None,
) -> IssuedSession | None:
    return _consume_temp_password(
        SqlAlchemyIdentityRepository(db),
        employee_no=employee_no,
        temp_password=temp_password,
        dependencies=_dependencies(dependencies),
    )


def complete_password_setup(
    db: Connection,
    *,
    bootstrap_token: str,
    password: str,
    dependencies: IdentityDependencies | None = None,
) -> None:
    _complete_password_setup(
        SqlAlchemyIdentityRepository(db),
        bootstrap_token=bootstrap_token,
        password=password,
        dependencies=_dependencies(dependencies),
    )


def enroll_totp(
    db: Connection,
    *,
    bootstrap_token: str,
    dependencies: IdentityDependencies | None = None,
) -> TotpEnrollment:
    return _enroll_totp(
        SqlAlchemyIdentityRepository(db),
        bootstrap_token=bootstrap_token,
        dependencies=_dependencies(dependencies),
    )


def confirm_totp(
    db: Connection,
    *,
    bootstrap_token: str,
    code: str,
    dependencies: IdentityDependencies | None = None,
) -> IssuedSession:
    return _confirm_totp(
        SqlAlchemyIdentityRepository(db),
        bootstrap_token=bootstrap_token,
        code=code,
        dependencies=_dependencies(dependencies),
    )


def login_password_step(
    db: Connection,
    *,
    employee_no: str,
    password: str,
    source: str,
    dependencies: IdentityDependencies | None = None,
) -> LoginChallenge | IssuedSession:
    return _login_password_step(
        SqlAlchemyIdentityRepository(db),
        employee_no=employee_no,
        password=password,
        source=source,
        dependencies=_dependencies(dependencies),
    )


def login_totp_step(
    db: Connection,
    *,
    challenge_token: str,
    code: str,
    dependencies: IdentityDependencies | None = None,
) -> IssuedSession:
    return _login_totp_step(
        SqlAlchemyIdentityRepository(db),
        challenge_token=challenge_token,
        code=code,
        dependencies=_dependencies(dependencies),
    )


def validate_session(
    db: Connection,
    *,
    raw_token: str,
    dependencies: IdentityDependencies | None = None,
) -> SessionPrincipal | None:
    return _validate_session(
        SqlAlchemyIdentityRepository(db),
        raw_token=raw_token,
        dependencies=_dependencies(dependencies),
    )


def logout(
    db: Connection,
    *,
    raw_token: str,
    dependencies: IdentityDependencies | None = None,
) -> bool:
    return _logout(
        SqlAlchemyIdentityRepository(db),
        raw_token=raw_token,
        dependencies=_dependencies(dependencies),
    )


def revoke_sessions_for(
    db: Connection,
    *,
    account_id: str,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies | None = None,
) -> int:
    return _revoke_sessions_for(
        SqlAlchemyIdentityRepository(db),
        account_id=account_id,
        actor=actor,
        reason=reason,
        dependencies=_dependencies(dependencies),
    )


def set_account_status(
    db: Connection,
    *,
    account_id: str,
    status: AccountStatus,
    expected_version: int,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies | None = None,
) -> AccountDto:
    return _set_account_status(
        SqlAlchemyIdentityRepository(db),
        account_id=account_id,
        status=status,
        expected_version=expected_version,
        actor=actor,
        reason=reason,
        dependencies=_dependencies(dependencies),
    )


__all__ = [
    "AccountConflict",
    "AccountDto",
    "AccountNotFound",
    "AccountStatus",
    "AuthenticationFailed",
    "DefaultEffectivePolicy",
    "EffectiveIdentityPolicy",
    "EffectivePolicyPort",
    "IdentityDependencies",
    "InvalidAccountTransition",
    "IssuedSession",
    "LastEffectiveSuperAdmin",
    "LoginBackoffActive",
    "LoginChallenge",
    "PasswordFloorViolation",
    "Principal",
    "SessionKind",
    "SessionPrincipal",
    "StaleAccountVersion",
    "TotpChallengeFailed",
    "TotpEnrollment",
    "complete_password_setup",
    "confirm_totp",
    "consume_temp_password",
    "create_account",
    "enroll_totp",
    "ensure_account_transition_allowed",
    "ensure_effective_super_admin_remains",
    "issue_temp_password",
    "login_password_step",
    "login_totp_step",
    "logout",
    "revoke_sessions_for",
    "set_account_status",
    "validate_session",
]
