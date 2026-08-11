from collections.abc import Callable
from typing import Any, Protocol

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Connection, Engine

PLATFORM = object()
ScopeResolver = object | Callable[[Request], object]


class AuthorizationRuntime(Protocol):
    @property
    def engine(self) -> Engine: ...


class DecisionView(Protocol):
    @property
    def code(self) -> object: ...

    @property
    def principal(self) -> object | None: ...


PrincipalResolver = Callable[[AuthorizationRuntime, Connection, str], DecisionView]
CapabilityResolver = Callable[
    [AuthorizationRuntime, Connection, str, str, object],
    DecisionView,
]


def _raw_token(request: Request) -> str:
    token = request.cookies.get("ep_session")
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    return token


def _principal_or_problem(decision: DecisionView) -> Any:
    code = getattr(decision.code, "value", str(decision.code))
    if code == "UNAUTHENTICATED":
        raise HTTPException(status_code=401, detail="Authentication required")
    if code == "DENIED":
        raise HTTPException(status_code=403, detail="Forbidden")
    if code == "UNAVAILABLE":
        raise HTTPException(status_code=503, detail="Authorization unavailable")
    if decision.principal is None:
        raise HTTPException(status_code=503, detail="Authorization unavailable")
    return decision.principal


def current_principal(
    runtime_provider: Callable[[], AuthorizationRuntime],
    resolver: PrincipalResolver,
) -> Callable[[Request], Any]:
    def dependency(request: Request) -> Any:
        raw_token = _raw_token(request)
        runtime = runtime_provider()
        with runtime.engine.begin() as db:
            decision = resolver(runtime, db, raw_token)
        return _principal_or_problem(decision)

    return dependency


def require_capability(
    capability: str,
    scope: ScopeResolver = PLATFORM,
    *,
    runtime_provider: Callable[[], AuthorizationRuntime],
    resolver: CapabilityResolver,
) -> object:
    def dependency(request: Request) -> Any:
        raw_token = _raw_token(request)
        runtime = runtime_provider()
        resolved_scope = scope(request) if callable(scope) else scope
        with runtime.engine.begin() as db:
            decision = resolver(
                runtime,
                db,
                raw_token,
                capability,
                resolved_scope,
            )
        return _principal_or_problem(decision)

    return Depends(dependency)


__all__ = [
    "AuthorizationRuntime",
    "CapabilityResolver",
    "DecisionView",
    "PLATFORM",
    "PrincipalResolver",
    "ScopeResolver",
    "current_principal",
    "require_capability",
]
