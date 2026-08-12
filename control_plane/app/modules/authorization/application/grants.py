from datetime import datetime
from typing import Any

from control_plane.app.modules.authorization.application.common import (
    actor_id,
    audit,
    grant_dto,
)
from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
)
from control_plane.app.modules.authorization.domain import (
    GrantDto,
    GrantNotFound,
    InitialProvisioningDenied,
    InvalidGrant,
    Scope,
    StaleGrantVersion,
)
from control_plane.app.modules.authorization.ports import AuthorizationRepository
from control_plane.app.shared.idempotency import (
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)

INITIAL_ADMIN_CAPABILITIES = (
    "audit.read",
    "identity.account.manage",
    "platform.authorization.manage",
)
SYSTEM_BOOTSTRAP_ACTOR = "SYSTEM_BOOTSTRAP"


class _SystemBootstrapActor:
    account_id = SYSTEM_BOOTSTRAP_ACTOR


def provision_initial_admin_grants(
    repository: AuthorizationRepository,
    *,
    principal_id: str,
    command_id: str,
    dependencies: AuthorizationDependencies,
) -> list[GrantDto]:
    principal_id = principal_id.strip()
    command_id = command_id.strip()
    if not principal_id or not command_id:
        raise InitialProvisioningDenied("initial provisioning identity must not be blank")
    operation = "initial_admin_provisioning"
    material = dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=operation,
        method="CLI",
        path="control_plane.tools.bootstrap_admin",
        body={"principalId": principal_id, "capabilities": list(INITIAL_ADMIN_CAPABILITIES)},
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        repository.lock_initial_provisioning()
        if repository.any_grants():
            audit(
                repository,
                dependencies=dependencies,
                actor=SYSTEM_BOOTSTRAP_ACTOR,
                action="authorization.initial_provisioning",
                target_type="authorization_principal",
                target_id=principal_id,
                result="DENIED",
                reason="initial provisioning already closed",
                correlation_id=command_id,
            )
            return IdempotentResponse(
                status_code=409,
                body={"denial": "initial provisioning already closed"},
            )
        created = [
            grant(
                repository,
                principal_id=principal_id,
                capability=capability,
                scope=Scope.platform(),
                actor=_SystemBootstrapActor(),
                reason=f"initial environment provisioning; commandId={command_id}",
                source="SYSTEM_BOOTSTRAP",
                dependencies=dependencies,
                correlation_id=command_id,
            )
            for capability in INITIAL_ADMIN_CAPABILITIES
        ]
        return IdempotentResponse(
            status_code=200,
            body={"grantIds": [item.id for item in created]},
        )

    execution = execute_idempotent(
        repository,
        actor=SYSTEM_BOOTSTRAP_ACTOR,
        operation=operation,
        key=command_id,
        fingerprint=fingerprint,
        command=command,
        now=dependencies.clock.now,
        new_id=dependencies.random.uuid4,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    if execution.response.status_code != 200:
        raise InitialProvisioningDenied(str(execution.response.body["denial"]))
    rows = []
    for grant_id in execution.response.body["grantIds"]:
        row = repository.grant_by_id(str(grant_id))
        if row is None:
            raise InitialProvisioningDenied("initial provisioning facts are unavailable")
        rows.append(grant_dto(row))
    return rows


def grant(
    repository: AuthorizationRepository,
    *,
    principal_id: str,
    capability: str,
    scope: Scope,
    actor: Any,
    reason: str,
    dependencies: AuthorizationDependencies,
    source: str = "MANUAL",
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    correlation_id: str | None = None,
) -> GrantDto:
    principal_id = principal_id.strip()
    capability = capability.strip()
    source = source.strip()
    reason = reason.strip()
    if not principal_id or not capability or not source or not reason:
        raise InvalidGrant("grant fields must not be blank")
    if valid_from is not None and valid_to is not None and valid_to <= valid_from:
        raise InvalidGrant("grant validity must be a positive half-open interval")
    now = dependencies.clock.now()
    row = repository.insert_grant(
        id=str(dependencies.random.uuid4()),
        principal_id=principal_id,
        capability=capability,
        scope_type=scope.scope_type.value,
        scope_id=scope.scope_id,
        source=source,
        valid_from=valid_from,
        valid_to=valid_to,
        now=now,
    )
    version = repository.bump_principal_version(principal_id, now)
    created = grant_dto(row)
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_id(actor),
        action="authorization.grant.created",
        target_type="GRANT",
        target_id=created.id,
        result="SUCCESS",
        reason=(
            f"{reason}; principal={principal_id}; capability={capability}; "
            f"scope={scope.scope_type.value}:{scope.scope_id or '-'}; "
            f"authorizationVersion={version['version']}"
        ),
        correlation_id=correlation_id,
    )
    return created


def revoke(
    repository: AuthorizationRepository,
    *,
    grant_id: str,
    expected_version: int,
    actor: Any,
    reason: str,
    dependencies: AuthorizationDependencies,
) -> GrantDto:
    reason = reason.strip()
    if not reason:
        raise InvalidGrant("revoke reason must not be blank")
    row = repository.grant_by_id(grant_id, for_update=True)
    if row is None:
        raise GrantNotFound(grant_id)
    if int(row["version"]) != expected_version or row["status"] != "ACTIVE":
        raise StaleGrantVersion(grant_id)
    actor_account_id = actor_id(actor)
    now = dependencies.clock.now()
    updated = repository.revoke_grant(
        grant_id=grant_id,
        expected_version=expected_version,
        actor_id=actor_account_id,
        reason=reason,
        now=now,
    )
    if updated is None:
        raise StaleGrantVersion(grant_id)
    version = repository.bump_principal_version(str(row["principal_id"]), now)
    revoked = grant_dto(updated)
    audit(
        repository,
        dependencies=dependencies,
        actor=actor_account_id,
        action="authorization.grant.revoked",
        target_type="GRANT",
        target_id=grant_id,
        result="SUCCESS",
        reason=(
            f"{reason}; principal={row['principal_id']}; beforeVersion={expected_version}; "
            f"afterVersion={revoked.version}; authorizationVersion={version['version']}"
        ),
    )
    return revoked


def effective_grants(
    repository: AuthorizationRepository,
    *,
    principal_id: str,
    capability: str | None,
    scope: Scope | None,
    dependencies: AuthorizationDependencies,
) -> list[GrantDto]:
    return [
        grant_dto(row)
        for row in repository.effective_grants(
            principal_id=principal_id,
            capability=capability,
            scope_type=scope.scope_type.value if scope is not None else None,
            scope_id=scope.scope_id if scope is not None else None,
            now=dependencies.clock.now(),
        )
    ]


def list_grants(repository: AuthorizationRepository) -> list[GrantDto]:
    return [grant_dto(row) for row in repository.list_grants()]
