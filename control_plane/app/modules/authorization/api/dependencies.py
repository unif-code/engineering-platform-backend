from collections.abc import Callable
from typing import Any, cast

from fastapi import Request
from sqlalchemy import Connection

from control_plane.app.modules.authorization import (
    AuthorizationDecision,
    Scope,
    authorize,
    resolve_principal,
)
from control_plane.app.modules.authorization.api.runtime import AuthorizationHttpRuntime
from control_plane.app.shared.api import authz as shared_authz

ScopeResolver = Scope | Callable[[Request], Scope]
PLATFORM = Scope.platform()


def _runtime(value: shared_authz.AuthorizationRuntime) -> AuthorizationHttpRuntime:
    return cast(AuthorizationHttpRuntime, value)


def _resolve_principal(
    runtime: shared_authz.AuthorizationRuntime,
    db: Connection,
    raw_token: str,
) -> shared_authz.DecisionView:
    concrete = _runtime(runtime)
    return cast(
        shared_authz.DecisionView,
        resolve_principal(
            db,
            raw_token=raw_token,
            dependencies=concrete.dependencies,
            decision_dependencies=concrete.decision_dependencies,
        ),
    )


def _authorize(
    runtime: shared_authz.AuthorizationRuntime,
    db: Connection,
    raw_token: str,
    capability: str,
    scope: object,
) -> shared_authz.DecisionView:
    concrete = _runtime(runtime)
    resolved_scope = PLATFORM if scope is shared_authz.PLATFORM else cast(Scope, scope)
    decision: AuthorizationDecision = authorize(
        db,
        raw_token=raw_token,
        capability=capability,
        scope=resolved_scope,
        dependencies=concrete.dependencies,
        decision_dependencies=concrete.decision_dependencies,
    )
    return cast(shared_authz.DecisionView, decision)


def current_principal(
    runtime_provider: Callable[[], AuthorizationHttpRuntime],
) -> Callable[[Request], Any]:
    provider = cast(Callable[[], shared_authz.AuthorizationRuntime], runtime_provider)
    return shared_authz.current_principal(provider, _resolve_principal)


def require_capability(
    capability: str,
    scope: ScopeResolver = PLATFORM,
    *,
    runtime_provider: Callable[[], AuthorizationHttpRuntime],
) -> object:
    provider = cast(Callable[[], shared_authz.AuthorizationRuntime], runtime_provider)
    return shared_authz.require_capability(
        capability,
        scope,
        runtime_provider=provider,
        resolver=_authorize,
    )
