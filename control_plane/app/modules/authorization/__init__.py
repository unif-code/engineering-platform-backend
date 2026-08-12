"""Public authorization facade; other modules must not import authorization internals."""

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.authorization.application.decisions import authorize as _authorize
from control_plane.app.modules.authorization.application.decisions import (
    principal_has_capability as _principal_has_capability,
)
from control_plane.app.modules.authorization.application.decisions import (
    resolve_principal as _resolve_principal,
)
from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
    DecisionDependencies,
)
from control_plane.app.modules.authorization.application.fence import (
    bump_version as _bump_version,
)
from control_plane.app.modules.authorization.application.fence import (
    clear_fence as _clear_fence,
)
from control_plane.app.modules.authorization.application.fence import (
    mark_fence as _mark_fence,
)
from control_plane.app.modules.authorization.application.fence import (
    principal_version as _principal_version,
)
from control_plane.app.modules.authorization.application.grants import (
    effective_grants as _effective_grants,
)
from control_plane.app.modules.authorization.application.grants import grant as _grant
from control_plane.app.modules.authorization.application.grants import (
    list_grants as _list_grants,
)
from control_plane.app.modules.authorization.application.grants import (
    provision_initial_admin_grants as _provision_initial_admin_grants,
)
from control_plane.app.modules.authorization.application.grants import revoke as _revoke
from control_plane.app.modules.authorization.application.orchestration import (
    SecurityChangeOrchestrator,
    SecurityChangeSource,
    SecurityChangeTicket,
)
from control_plane.app.modules.authorization.domain import (
    PLATFORM_CONFIGURATION_MANAGE,
    PLATFORM_SUPER_ADMIN_MANAGE,
    RESERVED_PLATFORM_CAPABILITIES,
    AuthorizationDecision,
    AuthorizationDenied,
    AuthorizationError,
    AuthorizationPrincipal,
    AuthorizationUnavailable,
    DecisionCode,
    GrantDto,
    GrantNotFound,
    GrantStatus,
    InitialProvisioningDenied,
    InvalidGrant,
    PrincipalVersionDto,
    Scope,
    ScopedCapability,
    ScopeType,
    StaleGrantVersion,
)


def grant(
    db: Connection,
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
    return _grant(
        dependencies.repository_factory(db),
        principal_id=principal_id,
        capability=capability,
        scope=scope,
        actor=actor,
        reason=reason,
        source=source,
        valid_from=valid_from,
        valid_to=valid_to,
        correlation_id=correlation_id,
        dependencies=dependencies,
    )


def provision_initial_admin_grants(
    db: Connection,
    *,
    principal_id: str,
    command_id: str,
    dependencies: AuthorizationDependencies,
) -> list[GrantDto]:
    return _provision_initial_admin_grants(
        dependencies.repository_factory(db),
        principal_id=principal_id,
        command_id=command_id,
        dependencies=dependencies,
    )


def revoke(
    db: Connection,
    *,
    grant_id: str,
    expected_version: int,
    actor: Any,
    reason: str,
    dependencies: AuthorizationDependencies,
) -> GrantDto:
    return _revoke(
        dependencies.repository_factory(db),
        grant_id=grant_id,
        expected_version=expected_version,
        actor=actor,
        reason=reason,
        dependencies=dependencies,
    )


def effective_grants(
    db: Connection,
    *,
    principal_id: str,
    capability: str | None = None,
    scope: Scope | None = None,
    dependencies: AuthorizationDependencies,
) -> list[GrantDto]:
    return _effective_grants(
        dependencies.repository_factory(db),
        principal_id=principal_id,
        capability=capability,
        scope=scope,
        dependencies=dependencies,
    )


def list_grants(db: Connection, *, dependencies: AuthorizationDependencies) -> list[GrantDto]:
    return _list_grants(dependencies.repository_factory(db))


def mark_fence(
    db: Connection,
    *,
    account_ids: Iterable[str],
    reason: str,
    dependencies: AuthorizationDependencies,
) -> dict[str, int]:
    return _mark_fence(
        dependencies.repository_factory(db),
        account_ids=account_ids,
        reason=reason,
        dependencies=dependencies,
    )


def clear_fence(
    db: Connection,
    *,
    generations: Mapping[str, int],
    dependencies: AuthorizationDependencies,
) -> set[str]:
    return _clear_fence(
        dependencies.repository_factory(db),
        generations=generations,
        dependencies=dependencies,
    )


def bump_version(
    db: Connection,
    *,
    account_id: str,
    dependencies: AuthorizationDependencies,
) -> PrincipalVersionDto:
    return _bump_version(
        dependencies.repository_factory(db),
        account_id=account_id,
        dependencies=dependencies,
    )


def principal_version(
    db: Connection,
    *,
    account_id: str,
    dependencies: AuthorizationDependencies,
) -> PrincipalVersionDto | None:
    return _principal_version(
        dependencies.repository_factory(db),
        account_id=account_id,
    )


def authorize(
    db: Connection,
    *,
    raw_token: str,
    capability: str,
    scope: Scope,
    dependencies: AuthorizationDependencies,
    decision_dependencies: DecisionDependencies,
) -> AuthorizationDecision:
    return _authorize(
        dependencies.repository_factory(db),
        raw_token=raw_token,
        capability=capability,
        scope=scope,
        dependencies=dependencies,
        decision_dependencies=decision_dependencies,
    )


def resolve_principal(
    db: Connection,
    *,
    raw_token: str,
    dependencies: AuthorizationDependencies,
    decision_dependencies: DecisionDependencies,
) -> AuthorizationDecision:
    return _resolve_principal(
        dependencies.repository_factory(db),
        raw_token=raw_token,
        dependencies=dependencies,
        decision_dependencies=decision_dependencies,
    )


def principal_has_capability(
    db: Connection,
    *,
    principal: AuthorizationPrincipal,
    capability: str,
    scope: Scope,
    dependencies: AuthorizationDependencies,
    decision_dependencies: DecisionDependencies | None = None,
) -> bool:
    return _principal_has_capability(
        dependencies.repository_factory(db),
        principal=principal,
        capability=capability,
        scope=scope,
        dependencies=dependencies,
        decision_dependencies=decision_dependencies,
    )


__all__ = [
    "AuthorizationDenied",
    "AuthorizationDependencies",
    "AuthorizationDecision",
    "AuthorizationError",
    "AuthorizationPrincipal",
    "AuthorizationUnavailable",
    "DecisionCode",
    "DecisionDependencies",
    "GrantDto",
    "GrantNotFound",
    "GrantStatus",
    "InvalidGrant",
    "InitialProvisioningDenied",
    "PLATFORM_CONFIGURATION_MANAGE",
    "PLATFORM_SUPER_ADMIN_MANAGE",
    "PrincipalVersionDto",
    "RESERVED_PLATFORM_CAPABILITIES",
    "Scope",
    "ScopedCapability",
    "SecurityChangeOrchestrator",
    "SecurityChangeSource",
    "SecurityChangeTicket",
    "ScopeType",
    "StaleGrantVersion",
    "bump_version",
    "authorize",
    "clear_fence",
    "effective_grants",
    "grant",
    "list_grants",
    "mark_fence",
    "principal_version",
    "principal_has_capability",
    "provision_initial_admin_grants",
    "revoke",
    "resolve_principal",
]
