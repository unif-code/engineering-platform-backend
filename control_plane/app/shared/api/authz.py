from collections.abc import Callable
from typing import Annotated, Any, Protocol

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyCookie
from sqlalchemy import Connection, Engine

PLATFORM = object()
EP_SESSION_COOKIE = APIKeyCookie(
    name="ep_session",
    scheme_name="EpSessionCookie",
    auto_error=False,
)
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


def _raw_token(token: str | None) -> str:
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
    def dependency(
        request: Request,
        cookie_token: Annotated[str | None, Security(EP_SESSION_COOKIE)] = None,
    ) -> Any:
        raw_token = _raw_token(cookie_token)
        try:
            runtime = runtime_provider()
            with runtime.engine.begin() as db:
                decision = resolver(runtime, db, raw_token)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Authorization unavailable",
            ) from exc
        return _principal_or_problem(decision)

    return dependency


def require_capability(
    capability: str,
    scope: ScopeResolver = PLATFORM,
    *,
    runtime_provider: Callable[[], AuthorizationRuntime],
    resolver: CapabilityResolver,
) -> object:
    def dependency(
        request: Request,
        cookie_token: Annotated[str | None, Security(EP_SESSION_COOKIE)] = None,
    ) -> Any:
        raw_token = _raw_token(cookie_token)
        try:
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
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Authorization unavailable",
            ) from exc
        return _principal_or_problem(decision)

    return Depends(dependency)


__all__ = [
    "AuthorizationRuntime",
    "CapabilityResolver",
    "DecisionView",
    "EP_SESSION_COOKIE",
    "PLATFORM",
    "PrincipalResolver",
    "ScopeResolver",
    "current_principal",
    "require_capability",
]
