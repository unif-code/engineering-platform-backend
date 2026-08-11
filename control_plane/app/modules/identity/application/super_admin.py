from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from control_plane.app.modules.identity.application.accounts import (
    _dto,
    _issue_temp,
    create_account,
)
from control_plane.app.modules.identity.application.common import audit, token_hash
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.idempotency import (
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from control_plane.app.modules.identity.application.security_change import (
    identity_change_source,
    notify_identity_change,
)
from control_plane.app.modules.identity.application.sessions import finalize_session_revocations
from control_plane.app.modules.identity.domain.account import (
    AccountDto,
    AccountStatus,
    ensure_effective_super_admin_remains,
)
from control_plane.app.modules.identity.domain.errors import (
    AccountNotFound,
    LastEffectiveSuperAdmin,
    StaleAccountVersion,
    SuperAdminBootstrapConflict,
    SuperAdminConflict,
    SuperAdminPermissionDenied,
    SuperAdminRecoveryDenied,
    TotpChallengeFailed,
)
from control_plane.app.modules.identity.domain.models import Principal
from control_plane.app.modules.identity.ports.repository import IdentityRepository
from control_plane.app.shared.security import unseal, verify_totp

SYSTEM_BOOTSTRAP = Principal(employee_id="SYSTEM_BOOTSTRAP", name="System Bootstrap")
SYSTEM_RECOVERY = Principal(employee_id="SYSTEM_RECOVERY", name="System Recovery")
SUPER_ADMIN_RECOVERY_SCOPE = "SUPER_ADMIN_AUTHENTICATION"
_CHALLENGE_TTL = timedelta(minutes=5)
SuperAdminOperation = Literal["ADD", "REMOVE"]


@dataclass(frozen=True, slots=True)
class SuperAdminCliExecution:
    account: AccountDto
    temporary_password: str
    correlation_id: str
    idempotency_key: str
    replayed: bool
    tickets: tuple[Any, ...]


def _purpose(operation: SuperAdminOperation) -> str:
    return f"SUPER_ADMIN_{operation}"


def _actor(row: Mapping[str, Any]) -> Principal:
    return Principal(
        employee_id=str(row["employee_no"]),
        name=str(row["display_name"]),
    )


def _is_effective(row: Mapping[str, Any]) -> bool:
    return bool(
        row["is_super_admin"]
        and row["status"] == AccountStatus.ENABLED.value
        and row["password_hash"] is not None
        and row["totp_confirmed_at"] is not None
    )


def bootstrap_super_admin(
    repository: IdentityRepository,
    *,
    employee_no: str,
    display_name: str,
    dependencies: IdentityDependencies,
    correlation_id: str | None = None,
) -> tuple[AccountDto, str]:
    repository.lock_super_admin_invariant()
    if repository.any_super_admin():
        raise SuperAdminBootstrapConflict("a Super Admin already exists")
    account, temporary_password = create_account(
        repository,
        employee_no=employee_no,
        display_name=display_name,
        actor=SYSTEM_BOOTSTRAP,
        reason="environment Super Admin bootstrap",
        dependencies=dependencies,
        correlation_id=correlation_id,
    )
    updated = repository.update_super_admin(
        account.id,
        True,
        account.version,
        dependencies.clock.now(),
    )
    if updated is None:
        raise SuperAdminBootstrapConflict("Super Admin bootstrap lost account ownership")
    promoted = AccountDto(
        id=str(updated["id"]),
        employee_no=str(updated["employee_no"]),
        display_name=str(updated["display_name"]),
        profession=updated["profession"],
        status=updated["status"],
        password_set_at=updated["password_set_at"],
        totp_confirmed_at=updated["totp_confirmed_at"],
        is_super_admin=bool(updated["is_super_admin"]),
        version=int(updated["version"]),
    )
    audit(
        repository.db,
        dependencies=dependencies,
        actor=SYSTEM_BOOTSTRAP,
        action="identity.super_admin.bootstrapped",
        target_type="account",
        target_id=promoted.id,
        result="SUCCESS",
        reason="environment Super Admin bootstrap",
        correlation_id=correlation_id,
    )
    notify_identity_change(dependencies.on_auth_change, promoted.id)
    return promoted, temporary_password


def list_super_admins(repository: IdentityRepository) -> list[AccountDto]:
    return [_dto(row) for row in repository.list_super_admins()]


def issue_super_admin_challenge(
    repository: IdentityRepository,
    *,
    actor_account_id: str,
    operation: SuperAdminOperation,
    dependencies: IdentityDependencies,
) -> str:
    actor_row = repository.account_by_id(actor_account_id, for_update=True)
    if actor_row is None or not _is_effective(actor_row):
        raise SuperAdminPermissionDenied("current effective Super Admin required")
    now = dependencies.clock.now()
    policy = dependencies.policy.get_identity_policy(repository.db)
    purpose = _purpose(operation)
    attempts = repository.admin_challenge_attempts(
        actor_account_id,
        purpose,
        now - _CHALLENGE_TTL,
    )
    if attempts >= policy.totp_attempt_cap:
        audit(
            repository.db,
            dependencies=dependencies,
            actor=_actor(actor_row),
            action="identity.super_admin.challenge.issued",
            target_type="account",
            target_id=actor_account_id,
            result="DENIED",
            reason=f"purpose={purpose}; attempt limit exhausted",
        )
        raise TotpChallengeFailed("Super Admin TOTP attempt limit exhausted")
    raw_token = dependencies.random.token_urlsafe(32)
    repository.insert_admin_challenge(
        id=str(dependencies.random.uuid4()),
        token_hash=token_hash(raw_token),
        purpose=purpose,
        account_id=actor_account_id,
        actor_id=actor_account_id,
        issued_at=now,
        expires_at=now + _CHALLENGE_TTL,
        attempt_limit=policy.totp_attempt_cap,
    )
    audit(
        repository.db,
        dependencies=dependencies,
        actor=_actor(actor_row),
        action="identity.super_admin.challenge.issued",
        target_type="account",
        target_id=actor_account_id,
        result="SUCCESS",
        reason=f"purpose={purpose}",
    )
    return raw_token


def _consume_super_admin_challenge(
    repository: IdentityRepository,
    *,
    actor_account_id: str,
    operation: SuperAdminOperation,
    challenge_token: str,
    totp_code: str,
    dependencies: IdentityDependencies,
) -> Principal:
    now = dependencies.clock.now()
    challenge = repository.challenge_by_hash(token_hash(challenge_token), for_update=True)
    active = bool(
        challenge is not None
        and str(challenge["account_id"]) == actor_account_id
        and str(challenge["actor_id"]) == actor_account_id
        and challenge["purpose"] == _purpose(operation)
        and challenge["consumed_at"] is None
        and challenge["revoked_at"] is None
        and now < challenge["expires_at"]
        and challenge["attempt_count"] < challenge["attempt_limit"]
        and _is_effective(challenge)
        and challenge["totp_sealed"] is not None
    )
    step = None
    if active:
        material = dependencies.secret_manager.load()
        secret = unseal(challenge["totp_sealed"], material.totp_sealing_key).decode("ascii")
        step = verify_totp(secret, totp_code, last_used_step=challenge["totp_last_step"])
    if challenge is None:
        raise TotpChallengeFailed("invalid Super Admin TOTP challenge")
    actor = _actor(challenge)
    if step is None or not repository.update_totp_step(actor_account_id, step, now):
        repository.fail_challenge(str(challenge["challenge_id"]), now)
        audit(
            repository.db,
            dependencies=dependencies,
            actor=actor,
            action="identity.super_admin.challenge.verified",
            target_type="account",
            target_id=actor_account_id,
            result="DENIED",
            reason=f"purpose={_purpose(operation)}; invalid or replayed TOTP",
        )
        raise TotpChallengeFailed("invalid Super Admin TOTP challenge")
    if not repository.consume_challenge(str(challenge["challenge_id"]), now):
        raise TotpChallengeFailed("invalid Super Admin TOTP challenge")
    audit(
        repository.db,
        dependencies=dependencies,
        actor=actor,
        action="identity.super_admin.challenge.verified",
        target_type="account",
        target_id=actor_account_id,
        result="SUCCESS",
        reason=f"purpose={_purpose(operation)}",
    )
    return actor


def _change_super_admin(
    repository: IdentityRepository,
    *,
    target_account_id: str,
    actor_account_id: str,
    operation: SuperAdminOperation,
    challenge_token: str,
    totp_code: str,
    reason: str,
    expected_version: int,
    dependencies: IdentityDependencies,
) -> AccountDto:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("reason must not be blank")
    repository.lock_super_admin_invariant()
    actor = _consume_super_admin_challenge(
        repository,
        actor_account_id=actor_account_id,
        operation=operation,
        challenge_token=challenge_token,
        totp_code=totp_code,
        dependencies=dependencies,
    )
    desired = operation == "ADD"
    action = "identity.super_admin.added" if desired else "identity.super_admin.removed"
    try:
        target = repository.account_by_id(target_account_id, for_update=True)
        if target is None:
            raise AccountNotFound(target_account_id)
        if int(target["version"]) != expected_version:
            raise StaleAccountVersion(target_account_id)
        if bool(target["is_super_admin"]) is desired:
            raise SuperAdminConflict("Super Admin state already matches the request")
        if desired and (
            target["status"] != AccountStatus.ENABLED.value
            or target["password_hash"] is None
            or target["totp_confirmed_at"] is None
        ):
            raise SuperAdminConflict("target account must be enabled and fully initialized")
        if not desired:
            ensure_effective_super_admin_remains(
                is_super_admin=bool(target["is_super_admin"]),
                status=AccountStatus(target["status"]),
                password_initialized=target["password_hash"] is not None,
                totp_initialized=target["totp_confirmed_at"] is not None,
                other_effective_super_admins=repository.effective_super_admins_except(
                    target_account_id
                ),
            )
        now = dependencies.clock.now()
        updated = repository.update_super_admin(
            target_account_id,
            desired,
            int(target["version"]),
            now,
        )
        if updated is None:
            raise SuperAdminConflict("Super Admin state changed concurrently")
    except (
        AccountNotFound,
        LastEffectiveSuperAdmin,
        StaleAccountVersion,
        SuperAdminConflict,
    ) as error:
        audit(
            repository.db,
            dependencies=dependencies,
            actor=actor,
            action=action,
            target_type="account",
            target_id=target_account_id,
            result="DENIED",
            reason=f"{normalized_reason}; denial={type(error).__name__}",
        )
        raise
    revoked = repository.revoke_sessions(
        target_account_id,
        now,
        f"SUPER_ADMIN_{operation}",
    )
    finalize_session_revocations(
        repository,
        account_id=target_account_id,
        revoked_session_ids=revoked,
        actor=actor,
        reason=f"Super Admin {operation.lower()}",
        dependencies=dependencies,
        invoke_hook=False,
    )
    audit(
        repository.db,
        dependencies=dependencies,
        actor=actor,
        action=action,
        target_type="account",
        target_id=target_account_id,
        result="SUCCESS",
        reason=(
            f"{normalized_reason}; beforeVersion={target['version']}; "
            f"afterVersion={updated['version']}"
        ),
    )
    notify_identity_change(dependencies.on_auth_change, target_account_id)
    return _dto(updated)


def add_super_admin(
    repository: IdentityRepository,
    *,
    target_account_id: str,
    actor_account_id: str,
    challenge_token: str,
    totp_code: str,
    reason: str,
    expected_version: int,
    dependencies: IdentityDependencies,
) -> AccountDto:
    return _change_super_admin(
        repository,
        target_account_id=target_account_id,
        actor_account_id=actor_account_id,
        operation="ADD",
        challenge_token=challenge_token,
        totp_code=totp_code,
        reason=reason,
        expected_version=expected_version,
        dependencies=dependencies,
    )


def remove_super_admin(
    repository: IdentityRepository,
    *,
    target_account_id: str,
    actor_account_id: str,
    challenge_token: str,
    totp_code: str,
    reason: str,
    expected_version: int,
    dependencies: IdentityDependencies,
) -> AccountDto:
    return _change_super_admin(
        repository,
        target_account_id=target_account_id,
        actor_account_id=actor_account_id,
        operation="REMOVE",
        challenge_token=challenge_token,
        totp_code=totp_code,
        reason=reason,
        expected_version=expected_version,
        dependencies=dependencies,
    )


def recover_super_admin(
    repository: IdentityRepository,
    *,
    employee_no: str,
    reason: str,
    scope: str,
    expires_at: datetime,
    credentials_lost: bool,
    dependencies: IdentityDependencies,
    correlation_id: str | None = None,
) -> tuple[AccountDto, str]:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise SuperAdminRecoveryDenied("recovery reason is required")
    if scope != SUPER_ADMIN_RECOVERY_SCOPE:
        raise SuperAdminRecoveryDenied("recovery scope is not permitted")
    now = dependencies.clock.now()
    expiry = expires_at
    policy = dependencies.policy.get_identity_policy(repository.db)
    if expiry.tzinfo is None or not now < expiry <= now + policy.temp_credential_ttl:
        raise SuperAdminRecoveryDenied("recovery expiry is outside the permitted window")

    repository.lock_super_admin_invariant()
    target = repository.account_by_employee_no(employee_no, for_update=True)
    if target is None or not bool(target["is_super_admin"]):
        raise SuperAdminRecoveryDenied("target must be an existing Super Admin")
    if target["status"] != AccountStatus.DISABLED.value and not credentials_lost:
        raise SuperAdminRecoveryDenied("target is not unavailable")
    if not credentials_lost and repository.effective_super_admins_except(str(target["id"])) != 0:
        raise SuperAdminRecoveryDenied("another Super Admin can authenticate normally")

    account_id = str(target["id"])
    temporary_password = _issue_temp(
        repository,
        account_id=account_id,
        actor=SYSTEM_RECOVERY,
        now=now,
        dependencies=dependencies,
        expires_at=expiry,
    )
    updated = repository.reset_recovery_state(account_id, now)
    if updated is None:
        raise SuperAdminRecoveryDenied("recovery target changed concurrently")
    revoked = repository.revoke_sessions(account_id, now, "SUPER_ADMIN_RECOVERY")
    finalize_session_revocations(
        repository,
        account_id=account_id,
        revoked_session_ids=revoked,
        actor=SYSTEM_RECOVERY,
        reason="Super Admin recovery",
        dependencies=dependencies,
        invoke_hook=False,
        correlation_id=correlation_id,
    )
    audit(
        repository.db,
        dependencies=dependencies,
        actor=SYSTEM_RECOVERY,
        action="identity.temp_credential.issued",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason=(
            f"{normalized_reason}; scope={scope}; expiresAt={expiry.isoformat()}; "
            "source=BREAK_GLASS"
        ),
        correlation_id=correlation_id,
    )
    audit(
        repository.db,
        dependencies=dependencies,
        actor=SYSTEM_RECOVERY,
        action="identity.super_admin.recovered",
        target_type="account",
        target_id=account_id,
        result="SUCCESS",
        reason=(
            f"{normalized_reason}; scope={scope}; expiresAt={expiry.isoformat()}; "
            f"credentialsLost={str(credentials_lost).lower()}; "
            f"beforeVersion={target['version']}; afterVersion={updated['version']}"
        ),
        correlation_id=correlation_id,
    )
    notify_identity_change(dependencies.on_auth_change, account_id)
    return _dto(updated), temporary_password


def _cli_descriptor(
    *,
    operation: str,
    path: str,
    body: Mapping[str, object],
    dependencies: IdentityDependencies,
) -> tuple[str, str]:
    key = canonical_request_fingerprint(
        operation=operation,
        method="CLI",
        path=path,
        body=body,
        idempotency_sealing_key=(dependencies.secret_manager.load().idempotency_sealing_key),
    )
    return key, f"cli-{key[:32]}"


def _cli_response(
    account: AccountDto,
    temporary_password: str,
    correlation_id: str,
) -> IdempotentResponse:
    return IdempotentResponse(
        status_code=200,
        body={
            "account": account.model_dump(mode="json"),
            "temporaryPassword": temporary_password,
            "commandId": correlation_id,
        },
    )


def _cli_execution(
    response: IdempotentResponse,
    *,
    key: str,
    replayed: bool,
    tickets: tuple[Any, ...] = (),
) -> SuperAdminCliExecution:
    return SuperAdminCliExecution(
        account=AccountDto.model_validate(response.body["account"]),
        temporary_password=str(response.body["temporaryPassword"]),
        correlation_id=str(response.body["commandId"]),
        idempotency_key=key,
        replayed=replayed,
        tickets=tickets,
    )


def bootstrap_super_admin_cli(
    repository: IdentityRepository,
    *,
    employee_no: str,
    display_name: str,
    source_transaction_id: str,
    dependencies: IdentityDependencies,
) -> SuperAdminCliExecution:
    operation = "super_admin_bootstrap_cli"
    body: dict[str, object] = {
        "employeeNo": employee_no,
        "displayName": display_name,
    }
    key, correlation_id = _cli_descriptor(
        operation=operation,
        path="control_plane.tools.bootstrap_admin",
        body=body,
        dependencies=dependencies,
    )

    def command() -> IdempotentResponse:
        account, temporary_password = bootstrap_super_admin(
            repository,
            employee_no=employee_no,
            display_name=display_name,
            dependencies=dependencies,
            correlation_id=correlation_id,
        )
        return _cli_response(account, temporary_password, correlation_id)

    with identity_change_source(
        actor=SYSTEM_BOOTSTRAP.employee_id,
        operation=operation,
        idempotency_key=key,
        source_transaction_id=source_transaction_id,
    ) as source:
        execution = execute_idempotent(
            repository,
            actor=SYSTEM_BOOTSTRAP.employee_id,
            operation=operation,
            key=key,
            fingerprint=key,
            command=command,
            dependencies=dependencies,
            on_claimed=source.bind_claim,
        )
    return _cli_execution(
        execution.response,
        key=key,
        replayed=execution.replayed,
        tickets=tuple(source.tickets),
    )


def resolve_bootstrap_cli(
    repository: IdentityRepository,
    *,
    employee_no: str,
    display_name: str,
    dependencies: IdentityDependencies,
) -> SuperAdminCliExecution | None:
    operation = "super_admin_bootstrap_cli"
    body: dict[str, object] = {
        "employeeNo": employee_no,
        "displayName": display_name,
    }
    key, _correlation_id = _cli_descriptor(
        operation=operation,
        path="control_plane.tools.bootstrap_admin",
        body=body,
        dependencies=dependencies,
    )
    if (
        repository.idempotency_by_scope(
            SYSTEM_BOOTSTRAP.employee_id,
            operation,
            key,
            for_update=True,
        )
        is None
    ):
        return None

    def unexpected_execution() -> IdempotentResponse:
        raise RuntimeError("commit resolution must not execute a new command")

    execution = execute_idempotent(
        repository,
        actor=SYSTEM_BOOTSTRAP.employee_id,
        operation=operation,
        key=key,
        fingerprint=key,
        command=unexpected_execution,
        dependencies=dependencies,
    )
    return _cli_execution(execution.response, key=key, replayed=True)


def recover_super_admin_cli(
    repository: IdentityRepository,
    *,
    employee_no: str,
    reason: str,
    scope: str,
    expires_at: datetime,
    credentials_lost: bool,
    source_transaction_id: str,
    dependencies: IdentityDependencies,
) -> SuperAdminCliExecution:
    operation = "super_admin_recovery_cli"
    body: dict[str, object] = {
        "employeeNo": employee_no,
        "reason": reason.strip(),
        "scope": scope,
        "expiresAt": expires_at.isoformat(),
        "credentialsLost": credentials_lost,
    }
    key, correlation_id = _cli_descriptor(
        operation=operation,
        path="control_plane.tools.recovery",
        body=body,
        dependencies=dependencies,
    )

    def command() -> IdempotentResponse:
        account, temporary_password = recover_super_admin(
            repository,
            employee_no=employee_no,
            reason=reason,
            scope=scope,
            expires_at=expires_at,
            credentials_lost=credentials_lost,
            dependencies=dependencies,
            correlation_id=correlation_id,
        )
        return _cli_response(account, temporary_password, correlation_id)

    with identity_change_source(
        actor=SYSTEM_RECOVERY.employee_id,
        operation=operation,
        idempotency_key=key,
        source_transaction_id=source_transaction_id,
    ) as source:
        execution = execute_idempotent(
            repository,
            actor=SYSTEM_RECOVERY.employee_id,
            operation=operation,
            key=key,
            fingerprint=key,
            command=command,
            dependencies=dependencies,
            on_claimed=source.bind_claim,
        )
    return _cli_execution(
        execution.response,
        key=key,
        replayed=execution.replayed,
        tickets=tuple(source.tickets),
    )


def resolve_recovery_cli(
    repository: IdentityRepository,
    *,
    employee_no: str,
    reason: str,
    scope: str,
    expires_at: datetime,
    credentials_lost: bool,
    dependencies: IdentityDependencies,
) -> SuperAdminCliExecution | None:
    operation = "super_admin_recovery_cli"
    body: dict[str, object] = {
        "employeeNo": employee_no,
        "reason": reason.strip(),
        "scope": scope,
        "expiresAt": expires_at.isoformat(),
        "credentialsLost": credentials_lost,
    }
    key, _correlation_id = _cli_descriptor(
        operation=operation,
        path="control_plane.tools.recovery",
        body=body,
        dependencies=dependencies,
    )
    if (
        repository.idempotency_by_scope(
            SYSTEM_RECOVERY.employee_id,
            operation,
            key,
            for_update=True,
        )
        is None
    ):
        return None

    def unexpected_execution() -> IdempotentResponse:
        raise RuntimeError("commit resolution must not execute a new command")

    execution = execute_idempotent(
        repository,
        actor=SYSTEM_RECOVERY.employee_id,
        operation=operation,
        key=key,
        fingerprint=key,
        command=unexpected_execution,
        dependencies=dependencies,
    )
    return _cli_execution(execution.response, key=key, replayed=True)
