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
    InvalidGrant,
    Scope,
    StaleGrantVersion,
)
from control_plane.app.modules.authorization.ports import AuthorizationRepository


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
