"""Public identity facade; other modules must not import identity internals."""

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Connection

from control_plane.app.modules.identity.adapters.configuration_policy import (
    SqlAlchemyIdentityPolicyOwnerRepository,
)
from control_plane.app.modules.identity.application.accounts import (
    consume_temp_password as _consume_temp_password,
)
from control_plane.app.modules.identity.application.accounts import (
    create_account as _create_account,
)
from control_plane.app.modules.identity.application.accounts import (
    get_organization_account as _get_organization_account,
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
from control_plane.app.modules.identity.application.configuration_policy import (
    active_policy_snapshot as _active_policy_snapshot,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    archive_policy_candidates as _archive_policy_candidates,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    archive_policy_draft as _archive_policy_draft,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    claim_configuration_idempotency as _claim_configuration_idempotency,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    complete_configuration_idempotency as _complete_configuration_idempotency,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    configuration_idempotency_by_scope as _configuration_idempotency_by_scope,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    create_policy_draft as _create_policy_draft,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    effective_identity_policy as _effective_identity_policy,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    list_policy_versions as _list_policy_versions,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    locked_active_policy_snapshot as _locked_active_policy_snapshot,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    policy_catalog as _policy_catalog,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    policy_draft as _policy_draft,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    policy_version_snapshot as _policy_version_snapshot,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    preview_policy_candidate as _preview_policy_candidate,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    publish_policy_version as _publish_policy_version,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    save_policy_draft_preview as _save_policy_draft_preview,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    save_policy_draft_validation as _save_policy_draft_validation,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    update_policy_draft as _update_policy_draft,
)
from control_plane.app.modules.identity.application.configuration_policy import (
    validate_policy_candidate as _validate_policy_candidate,
)
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.security_change import (
    IdentityChangeSource,
    current_identity_change_source,
    identity_change_source,
)
from control_plane.app.modules.identity.application.sessions import logout as _logout
from control_plane.app.modules.identity.application.sessions import (
    revoke_sessions_for as _revoke_sessions_for,
)
from control_plane.app.modules.identity.application.sessions import (
    validate_session as _validate_session,
)
from control_plane.app.modules.identity.application.super_admin import (
    SuperAdminCliExecution,
)
from control_plane.app.modules.identity.application.super_admin import (
    add_super_admin as _add_super_admin,
)
from control_plane.app.modules.identity.application.super_admin import (
    bootstrap_super_admin as _bootstrap_super_admin,
)
from control_plane.app.modules.identity.application.super_admin import (
    bootstrap_super_admin_cli as _bootstrap_super_admin_cli,
)
from control_plane.app.modules.identity.application.super_admin import (
    issue_super_admin_challenge as _issue_super_admin_challenge,
)
from control_plane.app.modules.identity.application.super_admin import (
    list_super_admins as _list_super_admins,
)
from control_plane.app.modules.identity.application.super_admin import (
    record_super_admin_recovery_denial as _record_super_admin_recovery_denial,
)
from control_plane.app.modules.identity.application.super_admin import (
    recover_super_admin as _recover_super_admin,
)
from control_plane.app.modules.identity.application.super_admin import (
    recover_super_admin_cli as _recover_super_admin_cli,
)
from control_plane.app.modules.identity.application.super_admin import (
    remove_super_admin as _remove_super_admin,
)
from control_plane.app.modules.identity.application.super_admin import (
    resolve_bootstrap_cli as _resolve_bootstrap_cli,
)
from control_plane.app.modules.identity.application.super_admin import (
    resolve_recovery_cli as _resolve_recovery_cli,
)
from control_plane.app.modules.identity.application.super_admin import (
    verify_admin_totp as _verify_admin_totp,
)
from control_plane.app.modules.identity.domain.account import (
    AccountDto,
    AccountStatus,
    OrganizationAccountDto,
    ensure_account_transition_allowed,
    ensure_effective_super_admin_remains,
)
from control_plane.app.modules.identity.domain.configuration_policy import (
    OwnedPolicyDraft,
    OwnedPolicyKey,
    OwnedPolicyPreviewItem,
    OwnedPolicySnapshot,
    OwnedPolicySnapshotUnavailable,
    OwnedPolicyValidationIssue,
    OwnedPublishedPolicyVersion,
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
    SuperAdminBootstrapConflict,
    SuperAdminConflict,
    SuperAdminPermissionDenied,
    SuperAdminRecoveryDenied,
    TotpChallengeFailed,
)
from control_plane.app.modules.identity.domain.models import Principal
from control_plane.app.modules.identity.domain.policy import EffectiveIdentityPolicy
from control_plane.app.modules.identity.domain.session import (
    AuthChallengeState,
    AuthDenialCode,
    AuthenticationDenial,
    BootstrapDenial,
    BootstrapDenialCode,
    BootstrapPurpose,
    IssuedSession,
    LoginChallenge,
    SessionKind,
    SessionPrincipal,
    TotpEnrollment,
)
from control_plane.app.modules.identity.ports.policy import EffectivePolicyPort
from control_plane.app.modules.identity.ports.repository import IdentityRepositoryFactory


def policy_catalog(db: Connection, namespace: str) -> list[OwnedPolicyKey]:
    return _policy_catalog(SqlAlchemyIdentityPolicyOwnerRepository(db), namespace)


def claim_configuration_idempotency(db: Connection, **values: Any) -> bool:
    return _claim_configuration_idempotency(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        **values,
    )


def configuration_idempotency_by_scope(
    db: Connection,
    actor: str,
    operation: str,
    idempotency_key: str,
    *,
    for_update: bool = False,
) -> Any:
    return _configuration_idempotency_by_scope(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        actor,
        operation,
        idempotency_key,
        for_update=for_update,
    )


def complete_configuration_idempotency(
    db: Connection,
    record_id: str,
    **values: Any,
) -> bool:
    return _complete_configuration_idempotency(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        record_id,
        **values,
    )


def create_policy_draft(db: Connection, **values: Any) -> OwnedPolicyDraft:
    return _create_policy_draft(SqlAlchemyIdentityPolicyOwnerRepository(db), **values)


def policy_draft(
    db: Connection,
    draft_id: str,
    *,
    for_update: bool = False,
) -> OwnedPolicyDraft | None:
    return _policy_draft(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        draft_id,
        for_update=for_update,
    )


def update_policy_draft(
    db: Connection,
    draft_id: str,
    **values: Any,
) -> OwnedPolicyDraft | None:
    return _update_policy_draft(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        draft_id,
        **values,
    )


def save_policy_draft_validation(
    db: Connection,
    draft_id: str,
    **values: Any,
) -> OwnedPolicyDraft | None:
    return _save_policy_draft_validation(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        draft_id,
        **values,
    )


def save_policy_draft_preview(
    db: Connection,
    draft_id: str,
    **values: Any,
) -> OwnedPolicyDraft | None:
    return _save_policy_draft_preview(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        draft_id,
        **values,
    )


def preview_policy_candidate(
    db: Connection,
    namespace: str,
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[OwnedPolicyPreviewItem]:
    return _preview_policy_candidate(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        namespace,
        before=before,
        after=after,
    )


def active_policy_snapshot(db: Connection, namespace: str) -> OwnedPolicySnapshot:
    return _active_policy_snapshot(SqlAlchemyIdentityPolicyOwnerRepository(db), namespace)


def locked_active_policy_snapshot(db: Connection, namespace: str) -> OwnedPolicySnapshot:
    return _locked_active_policy_snapshot(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        namespace,
    )


def policy_version_snapshot(
    db: Connection,
    namespace: str,
    scope: str,
    version: int,
) -> OwnedPolicySnapshot | None:
    return _policy_version_snapshot(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        namespace,
        scope,
        version,
    )


def archive_policy_candidates(
    db: Connection,
    namespace: str,
    scope: str,
    *,
    cutoff: datetime,
    limit: int,
) -> list[OwnedPolicyDraft]:
    return _archive_policy_candidates(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        namespace,
        scope,
        cutoff=cutoff,
        limit=limit,
    )


def archive_policy_draft(db: Connection, **values: Any) -> bool:
    return _archive_policy_draft(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        **values,
    )


def list_policy_versions(
    db: Connection,
    namespace: str,
    scope: str,
    *,
    before_version: int | None,
    limit: int,
) -> list[OwnedPublishedPolicyVersion]:
    return _list_policy_versions(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        namespace,
        scope,
        before_version=before_version,
        limit=limit,
    )


def publish_policy_version(db: Connection, **values: Any) -> OwnedPublishedPolicyVersion | None:
    return _publish_policy_version(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        **values,
    )


def validate_policy_candidate(
    db: Connection,
    namespace: str,
    *,
    schema_revision: int,
    values: dict[str, Any],
) -> list[OwnedPolicyValidationIssue]:
    return _validate_policy_candidate(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        namespace,
        schema_revision=schema_revision,
        values=values,
    )


def effective_identity_policy(
    db: Connection,
    namespace: str = "identity",
) -> EffectiveIdentityPolicy:
    return _effective_identity_policy(
        SqlAlchemyIdentityPolicyOwnerRepository(db),
        namespace,
    )


def create_account(
    db: Connection,
    *,
    employee_no: str,
    display_name: str,
    actor: Principal,
    reason: str,
    profession: str | None = None,
    dependencies: IdentityDependencies,
) -> tuple[AccountDto, str]:
    return _create_account(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        display_name=display_name,
        actor=actor,
        reason=reason,
        profession=profession,
        dependencies=dependencies,
    )


def issue_temp_password(
    db: Connection,
    *,
    account_id: str,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies,
) -> str:
    return _issue_temp_password(
        dependencies.repository_factory(db),
        account_id=account_id,
        actor=actor,
        reason=reason,
        dependencies=dependencies,
    )


def get_organization_account(
    db: Connection,
    *,
    account_id: str,
    dependencies: IdentityDependencies,
) -> OrganizationAccountDto | None:
    return _get_organization_account(
        dependencies.repository_factory(db),
        account_id=account_id,
    )


def consume_temp_password(
    db: Connection,
    *,
    employee_no: str,
    temp_password: str,
    dependencies: IdentityDependencies,
) -> IssuedSession | None:
    return _consume_temp_password(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        temp_password=temp_password,
        dependencies=dependencies,
    )


def complete_password_setup(
    db: Connection,
    *,
    bootstrap_token: str,
    password: str,
    dependencies: IdentityDependencies,
) -> BootstrapDenial | None:
    return _complete_password_setup(
        dependencies.repository_factory(db),
        bootstrap_token=bootstrap_token,
        password=password,
        dependencies=dependencies,
    )


def enroll_totp(
    db: Connection,
    *,
    bootstrap_token: str,
    dependencies: IdentityDependencies,
) -> TotpEnrollment | BootstrapDenial:
    return _enroll_totp(
        dependencies.repository_factory(db),
        bootstrap_token=bootstrap_token,
        dependencies=dependencies,
    )


def confirm_totp(
    db: Connection,
    *,
    bootstrap_token: str,
    code: str,
    dependencies: IdentityDependencies,
) -> IssuedSession | BootstrapDenial | AuthenticationDenial:
    return _confirm_totp(
        dependencies.repository_factory(db),
        bootstrap_token=bootstrap_token,
        code=code,
        dependencies=dependencies,
    )


def login_password_step(
    db: Connection,
    *,
    employee_no: str,
    password: str,
    source: str,
    dependencies: IdentityDependencies,
) -> LoginChallenge | IssuedSession | AuthenticationDenial:
    return _login_password_step(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        password=password,
        source=source,
        dependencies=dependencies,
    )


def login_totp_step(
    db: Connection,
    *,
    challenge_token: str,
    code: str,
    dependencies: IdentityDependencies,
) -> IssuedSession | AuthenticationDenial:
    return _login_totp_step(
        dependencies.repository_factory(db),
        challenge_token=challenge_token,
        code=code,
        dependencies=dependencies,
    )


def validate_session(
    db: Connection,
    *,
    raw_token: str,
    dependencies: IdentityDependencies,
    touch_activity: bool = True,
) -> SessionPrincipal | None:
    return _validate_session(
        dependencies.repository_factory(db),
        raw_token=raw_token,
        dependencies=dependencies,
        touch_activity=touch_activity,
    )


def logout(
    db: Connection,
    *,
    raw_token: str,
    dependencies: IdentityDependencies,
) -> bool:
    return _logout(
        dependencies.repository_factory(db),
        raw_token=raw_token,
        dependencies=dependencies,
    )


def revoke_sessions_for(
    db: Connection,
    *,
    account_id: str,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies,
) -> int:
    return _revoke_sessions_for(
        dependencies.repository_factory(db),
        account_id=account_id,
        actor=actor,
        reason=reason,
        dependencies=dependencies,
    )


def verify_admin_totp(
    db: Connection,
    actor: str,
    code: str,
    *,
    purpose: Literal["POLICY_PUBLISH", "POLICY_ROLLBACK"],
    dependencies: IdentityDependencies,
) -> Principal:
    return _verify_admin_totp(
        dependencies.repository_factory(db),
        actor,
        code,
        purpose=purpose,
        dependencies=dependencies,
    )


def set_account_status(
    db: Connection,
    *,
    account_id: str,
    status: AccountStatus,
    expected_version: int,
    actor: Principal,
    reason: str,
    dependencies: IdentityDependencies,
) -> AccountDto:
    return _set_account_status(
        dependencies.repository_factory(db),
        account_id=account_id,
        status=status,
        expected_version=expected_version,
        actor=actor,
        reason=reason,
        dependencies=dependencies,
    )


def bootstrap_super_admin(
    db: Connection,
    *,
    employee_no: str,
    display_name: str,
    dependencies: IdentityDependencies,
) -> tuple[AccountDto, str]:
    return _bootstrap_super_admin(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        display_name=display_name,
        dependencies=dependencies,
    )


def bootstrap_super_admin_cli(
    db: Connection,
    *,
    employee_no: str,
    display_name: str,
    source_transaction_id: str,
    dependencies: IdentityDependencies,
) -> SuperAdminCliExecution:
    return _bootstrap_super_admin_cli(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        display_name=display_name,
        source_transaction_id=source_transaction_id,
        dependencies=dependencies,
    )


def resolve_bootstrap_cli(
    db: Connection,
    *,
    employee_no: str,
    display_name: str,
    dependencies: IdentityDependencies,
) -> SuperAdminCliExecution | None:
    return _resolve_bootstrap_cli(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        display_name=display_name,
        dependencies=dependencies,
    )


def list_super_admins(
    db: Connection,
    *,
    dependencies: IdentityDependencies,
) -> list[AccountDto]:
    return _list_super_admins(dependencies.repository_factory(db))


def issue_super_admin_challenge(
    db: Connection,
    *,
    actor_account_id: str,
    operation: str,
    dependencies: IdentityDependencies,
) -> str:
    if operation not in {"ADD", "REMOVE"}:
        raise ValueError("unsupported Super Admin operation")
    return _issue_super_admin_challenge(
        dependencies.repository_factory(db),
        actor_account_id=actor_account_id,
        operation=operation,  # type: ignore[arg-type]
        dependencies=dependencies,
    )


def add_super_admin(
    db: Connection,
    *,
    target_account_id: str,
    actor_account_id: str,
    challenge_token: str,
    totp_code: str,
    reason: str,
    expected_version: int,
    dependencies: IdentityDependencies,
) -> AccountDto:
    return _add_super_admin(
        dependencies.repository_factory(db),
        target_account_id=target_account_id,
        actor_account_id=actor_account_id,
        challenge_token=challenge_token,
        totp_code=totp_code,
        reason=reason,
        expected_version=expected_version,
        dependencies=dependencies,
    )


def remove_super_admin(
    db: Connection,
    *,
    target_account_id: str,
    actor_account_id: str,
    challenge_token: str,
    totp_code: str,
    reason: str,
    expected_version: int,
    dependencies: IdentityDependencies,
) -> AccountDto:
    return _remove_super_admin(
        dependencies.repository_factory(db),
        target_account_id=target_account_id,
        actor_account_id=actor_account_id,
        challenge_token=challenge_token,
        totp_code=totp_code,
        reason=reason,
        expected_version=expected_version,
        dependencies=dependencies,
    )


def recover_super_admin(
    db: Connection,
    *,
    employee_no: str,
    reason: str,
    scope: str,
    expires_at: datetime,
    credentials_lost: bool,
    dependencies: IdentityDependencies,
) -> tuple[AccountDto, str]:
    return _recover_super_admin(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        reason=reason,
        scope=scope,
        expires_at=expires_at,
        credentials_lost=credentials_lost,
        dependencies=dependencies,
    )


def recover_super_admin_cli(
    db: Connection,
    *,
    employee_no: str,
    reason: str,
    scope: str,
    expires_at: datetime,
    credentials_lost: bool,
    source_transaction_id: str,
    dependencies: IdentityDependencies,
) -> SuperAdminCliExecution:
    return _recover_super_admin_cli(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        reason=reason,
        scope=scope,
        expires_at=expires_at,
        credentials_lost=credentials_lost,
        source_transaction_id=source_transaction_id,
        dependencies=dependencies,
    )


def record_super_admin_recovery_denial(
    db: Connection,
    *,
    employee_no: str,
    reason_code: str,
    correlation_id: str,
    dependencies: IdentityDependencies,
) -> None:
    _record_super_admin_recovery_denial(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        reason_code=reason_code,
        correlation_id=correlation_id,
        dependencies=dependencies,
    )


def resolve_recovery_cli(
    db: Connection,
    *,
    employee_no: str,
    reason: str,
    scope: str,
    expires_at: datetime,
    credentials_lost: bool,
    dependencies: IdentityDependencies,
) -> SuperAdminCliExecution | None:
    return _resolve_recovery_cli(
        dependencies.repository_factory(db),
        employee_no=employee_no,
        reason=reason,
        scope=scope,
        expires_at=expires_at,
        credentials_lost=credentials_lost,
        dependencies=dependencies,
    )


__all__ = [
    "AccountConflict",
    "AccountDto",
    "AccountNotFound",
    "AccountStatus",
    "AuthenticationFailed",
    "AuthenticationDenial",
    "AuthChallengeState",
    "AuthDenialCode",
    "BootstrapPurpose",
    "BootstrapDenial",
    "BootstrapDenialCode",
    "EffectiveIdentityPolicy",
    "EffectivePolicyPort",
    "IdentityDependencies",
    "IdentityChangeSource",
    "IdentityRepositoryFactory",
    "InvalidAccountTransition",
    "IssuedSession",
    "LastEffectiveSuperAdmin",
    "LoginBackoffActive",
    "LoginChallenge",
    "OrganizationAccountDto",
    "OwnedPolicySnapshot",
    "OwnedPolicyKey",
    "OwnedPolicyPreviewItem",
    "OwnedPolicyDraft",
    "OwnedPolicySnapshotUnavailable",
    "OwnedPolicyValidationIssue",
    "OwnedPublishedPolicyVersion",
    "PasswordFloorViolation",
    "Principal",
    "SessionKind",
    "SessionPrincipal",
    "StaleAccountVersion",
    "SuperAdminBootstrapConflict",
    "SuperAdminCliExecution",
    "SuperAdminConflict",
    "SuperAdminPermissionDenied",
    "SuperAdminRecoveryDenied",
    "TotpChallengeFailed",
    "TotpEnrollment",
    "complete_password_setup",
    "bootstrap_super_admin",
    "bootstrap_super_admin_cli",
    "add_super_admin",
    "active_policy_snapshot",
    "archive_policy_candidates",
    "archive_policy_draft",
    "locked_active_policy_snapshot",
    "list_policy_versions",
    "effective_identity_policy",
    "claim_configuration_idempotency",
    "complete_configuration_idempotency",
    "configuration_idempotency_by_scope",
    "create_policy_draft",
    "policy_draft",
    "policy_catalog",
    "policy_version_snapshot",
    "publish_policy_version",
    "preview_policy_candidate",
    "save_policy_draft_preview",
    "save_policy_draft_validation",
    "update_policy_draft",
    "validate_policy_candidate",
    "confirm_totp",
    "current_identity_change_source",
    "consume_temp_password",
    "create_account",
    "enroll_totp",
    "ensure_account_transition_allowed",
    "ensure_effective_super_admin_remains",
    "issue_temp_password",
    "issue_super_admin_challenge",
    "identity_change_source",
    "get_organization_account",
    "login_password_step",
    "login_totp_step",
    "list_super_admins",
    "logout",
    "revoke_sessions_for",
    "remove_super_admin",
    "recover_super_admin",
    "recover_super_admin_cli",
    "record_super_admin_recovery_denial",
    "resolve_recovery_cli",
    "resolve_bootstrap_cli",
    "set_account_status",
    "validate_session",
    "verify_admin_totp",
]
