import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from control_plane.app.modules.identity import (
    AuthChallengeState,
    AuthDenialCode,
    AuthenticationDenial,
    BootstrapDenial,
    BootstrapPurpose,
    IdentityDependencies,
    LoginChallenge,
    PasswordFloorViolation,
    SessionKind,
    TotpChallengeFailed,
    complete_password_setup,
    confirm_totp,
    enroll_totp,
    login_password_step,
    login_totp_step,
    logout,
    validate_session,
)
from control_plane.app.modules.identity.api.auth_dto import (
    AuthenticatedDto,
    BootstrapPasswordRequestDto,
    BootstrapRequiredDto,
    BootstrapTotpConfirmRequestDto,
    LoggedOutDto,
    LoginRequestDto,
    PasswordSetDto,
    PasswordUpdatedDto,
    TotpEnrollmentDto,
    TotpRequestDto,
    TotpRequiredDto,
)
from control_plane.app.modules.identity.application.idempotency import (
    CookieReplay,
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from control_plane.app.modules.identity.domain.errors import AuthenticationFailed
from control_plane.app.shared.api.camel import CamelModel
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import PROBLEM_RESPONSES, problem_response
from control_plane.app.shared.security import assert_same_origin

SESSION_COOKIE = "ep_session"
_COOKIE_OPTIONS: dict[str, Any] = {
    "secure": True,
    "httponly": True,
    "samesite": "lax",
    "path": "/",
}
_AUTH_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {status: PROBLEM_RESPONSES[status] for status in (401, 403, 409, 422, 429, 500)},
)


@dataclass(frozen=True, slots=True)
class IdentityHttpRuntime:
    engine: Engine
    dependencies: IdentityDependencies


def _dto_body(dto: CamelModel) -> dict[str, Any]:
    return dict(dto.model_dump(mode="json", by_alias=True))


def _success(dto: CamelModel, *, cookie: CookieReplay | None = None) -> IdempotentResponse:
    return IdempotentResponse(status_code=200, body=_dto_body(dto), cookie=cookie)


def _problem(
    status: int,
    title: str,
    *,
    detail: str | None = None,
    extra: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> IdempotentResponse:
    body: dict[str, Any] = {"title": title, "status": status}
    if detail is not None:
        body["detail"] = detail
    if extra:
        body.update(extra)
    return IdempotentResponse(
        status_code=status,
        body=body,
        headers=dict(headers or {}),
        is_problem=True,
    )


def _authentication_denial(denial: AuthenticationDenial) -> IdempotentResponse:
    if denial.code is AuthDenialCode.BACKOFF_ACTIVE:
        retry_after = max(1, denial.retry_after_seconds or 1)
        return _problem(
            429,
            "Authentication temporarily unavailable",
            extra={"retryAfter": retry_after},
            headers={"Retry-After": str(retry_after)},
        )
    if (
        denial.code is AuthDenialCode.INVALID_CHALLENGE
        and denial.challenge_state is AuthChallengeState.TERMINAL
    ):
        return _problem(
            429,
            "Authentication challenge exhausted",
            extra={"retryAfter": 1},
            headers={"Retry-After": "1"},
        )
    return _problem(401, "Authentication failed")


def _render(response: IdempotentResponse) -> JSONResponse:
    if response.is_problem:
        semantic = dict(response.body)
        title = str(semantic.pop("title"))
        semantic.pop("status", None)
        detail_value = semantic.pop("detail", None)
        detail = str(detail_value) if detail_value is not None else None
        rendered = problem_response(
            response.status_code,
            title,
            detail=detail,
            extra=semantic,
            headers=response.headers,
        )
    else:
        rendered = JSONResponse(
            status_code=response.status_code,
            content=response.body,
            headers=response.headers,
        )
    if response.cookie is not None:
        if response.cookie.action == "set":
            assert response.cookie.value is not None
            rendered.set_cookie(SESSION_COOKIE, response.cookie.value, **_COOKIE_OPTIONS)
        else:
            rendered.delete_cookie(SESSION_COOKIE, **_COOKIE_OPTIONS)
    return rendered


def _actor_scope(prefix: str, raw_value: str | None) -> str:
    if raw_value is None:
        return f"{prefix}:absent"
    digest = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _fingerprint(
    operation: str,
    path: str,
    body: Mapping[str, object],
) -> str:
    return canonical_request_fingerprint(
        operation=operation,
        method="POST",
        path=path,
        body=body,
    )


def _execute(
    runtime: IdentityHttpRuntime,
    *,
    actor: str,
    operation: str,
    path: str,
    key: str,
    body: Mapping[str, object],
    command: Callable[[Any], IdempotentResponse],
) -> JSONResponse:
    try:
        with runtime.engine.begin() as db:
            repository = runtime.dependencies.repository_factory(db)
            execution = execute_idempotent(
                repository,
                actor=actor,
                operation=operation,
                key=key,
                fingerprint=_fingerprint(operation, path, body),
                command=lambda: command(db),
                dependencies=runtime.dependencies,
            )
    except IdempotencyConflict:
        return problem_response(409, "Idempotency conflict")
    except IdempotencyReplayUnavailable:
        return problem_response(409, "Idempotency replay unavailable")
    return _render(execution.response)


def create_auth_router(
    runtime_provider: Callable[[], IdentityHttpRuntime],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])

    @router.post(
        "/login",
        operation_id="auth_login",
        response_model=TotpRequiredDto | BootstrapRequiredDto,
        responses=_AUTH_RESPONSES,
    )
    def auth_login(
        body: LoginRequestDto,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
    ) -> Any:
        assert_same_origin(request)
        runtime = runtime_provider()
        source = request.client.host if request.client is not None else "unknown"
        body_data = body.model_dump(mode="json", by_alias=True)

        def command(db: Any) -> IdempotentResponse:
            result = login_password_step(
                db,
                employee_no=body.employee_no,
                password=body.password,
                source=source,
                dependencies=runtime.dependencies,
            )
            if isinstance(result, AuthenticationDenial):
                return _authentication_denial(result)
            if isinstance(result, LoginChallenge):
                return _success(TotpRequiredDto(challenge_token=result.challenge_token))
            return _success(
                BootstrapRequiredDto(),
                cookie=CookieReplay(action="set", value=result.raw_token),
            )

        return _execute(
            runtime,
            actor=f"employee:{body.employee_no}",
            operation="auth_login",
            path="/api/v1/auth/login",
            key=idempotency_key,
            body=body_data,
            command=command,
        )

    @router.post(
        "/totp",
        operation_id="auth_totp",
        response_model=AuthenticatedDto,
        responses=_AUTH_RESPONSES,
    )
    def auth_totp(
        body: TotpRequestDto,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
    ) -> Any:
        assert_same_origin(request)
        runtime = runtime_provider()

        def command(db: Any) -> IdempotentResponse:
            result = login_totp_step(
                db,
                challenge_token=body.challenge_token,
                code=body.code,
                dependencies=runtime.dependencies,
            )
            if isinstance(result, AuthenticationDenial):
                return _authentication_denial(result)
            return _success(
                AuthenticatedDto(),
                cookie=CookieReplay(action="set", value=result.raw_token),
            )

        return _execute(
            runtime,
            actor=_actor_scope("challenge", body.challenge_token),
            operation="auth_totp",
            path="/api/v1/auth/totp",
            key=idempotency_key,
            body=body.model_dump(mode="json", by_alias=True),
            command=command,
        )

    @router.post(
        "/logout",
        operation_id="auth_logout",
        response_model=LoggedOutDto,
        responses=_AUTH_RESPONSES,
    )
    def auth_logout(
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
    ) -> Any:
        assert_same_origin(request)
        runtime = runtime_provider()
        raw_token = request.cookies.get(SESSION_COOKIE)

        def command(db: Any) -> IdempotentResponse:
            if raw_token is not None:
                logout(
                    db,
                    raw_token=raw_token,
                    dependencies=runtime.dependencies,
                )
            return _success(LoggedOutDto(), cookie=CookieReplay(action="delete"))

        return _execute(
            runtime,
            actor=_actor_scope("session", raw_token),
            operation="auth_logout",
            path="/api/v1/auth/logout",
            key=idempotency_key,
            body={},
            command=command,
        )

    @router.post(
        "/bootstrap/password",
        operation_id="auth_bootstrap_password",
        response_model=PasswordSetDto | PasswordUpdatedDto,
        responses=_AUTH_RESPONSES,
    )
    def auth_bootstrap_password(
        body: BootstrapPasswordRequestDto,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
    ) -> Any:
        assert_same_origin(request)
        runtime = runtime_provider()
        raw_token = request.cookies.get(SESSION_COOKIE)

        def command(db: Any) -> IdempotentResponse:
            if raw_token is None:
                return _problem(401, "Authentication failed")
            principal = validate_session(
                db,
                raw_token=raw_token,
                dependencies=runtime.dependencies,
            )
            if principal is None or principal.session_kind is not SessionKind.BOOTSTRAP:
                return _problem(401, "Authentication failed")
            try:
                denial = complete_password_setup(
                    db,
                    bootstrap_token=raw_token,
                    password=body.password,
                    dependencies=runtime.dependencies,
                )
            except PasswordFloorViolation as exc:
                return _problem(
                    422,
                    "Password does not meet security requirements",
                    extra={"violations": list(exc.violations)},
                )
            except AuthenticationFailed:
                return _problem(401, "Authentication failed")
            if isinstance(denial, BootstrapDenial):
                return _problem(401, "Authentication failed")
            if principal.bootstrap_purpose is BootstrapPurpose.PASSWORD_EXPIRED:
                return _success(
                    PasswordUpdatedDto(),
                    cookie=CookieReplay(action="delete"),
                )
            return _success(PasswordSetDto())

        return _execute(
            runtime,
            actor=_actor_scope("session", raw_token),
            operation="auth_bootstrap_password",
            path="/api/v1/auth/bootstrap/password",
            key=idempotency_key,
            body=body.model_dump(mode="json", by_alias=True),
            command=command,
        )

    @router.post(
        "/bootstrap/totp/enroll",
        operation_id="auth_bootstrap_totp_enroll",
        response_model=TotpEnrollmentDto,
        responses=_AUTH_RESPONSES,
    )
    def auth_bootstrap_totp_enroll(
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
    ) -> Any:
        assert_same_origin(request)
        runtime = runtime_provider()
        raw_token = request.cookies.get(SESSION_COOKIE)

        def command(db: Any) -> IdempotentResponse:
            if raw_token is None:
                return _problem(401, "Authentication failed")
            try:
                result = enroll_totp(
                    db,
                    bootstrap_token=raw_token,
                    dependencies=runtime.dependencies,
                )
            except AuthenticationFailed:
                return _problem(401, "Authentication failed")
            if isinstance(result, BootstrapDenial):
                return _problem(401, "Authentication failed")
            return _success(TotpEnrollmentDto(provisioning_uri=result.provisioning_uri))

        return _execute(
            runtime,
            actor=_actor_scope("session", raw_token),
            operation="auth_bootstrap_totp_enroll",
            path="/api/v1/auth/bootstrap/totp/enroll",
            key=idempotency_key,
            body={},
            command=command,
        )

    @router.post(
        "/bootstrap/totp/confirm",
        operation_id="auth_bootstrap_totp_confirm",
        response_model=AuthenticatedDto,
        responses=_AUTH_RESPONSES,
    )
    def auth_bootstrap_totp_confirm(
        body: BootstrapTotpConfirmRequestDto,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
    ) -> Any:
        assert_same_origin(request)
        runtime = runtime_provider()
        raw_token = request.cookies.get(SESSION_COOKIE)

        def command(db: Any) -> IdempotentResponse:
            if raw_token is None:
                return _problem(401, "Authentication failed")
            try:
                result = confirm_totp(
                    db,
                    bootstrap_token=raw_token,
                    code=body.code,
                    dependencies=runtime.dependencies,
                )
            except (AuthenticationFailed, TotpChallengeFailed):
                return _problem(401, "Authentication failed")
            if isinstance(result, BootstrapDenial):
                return _problem(401, "Authentication failed")
            return _success(
                AuthenticatedDto(),
                cookie=CookieReplay(action="set", value=result.raw_token),
            )

        return _execute(
            runtime,
            actor=_actor_scope("session", raw_token),
            operation="auth_bootstrap_totp_confirm",
            path="/api/v1/auth/bootstrap/totp/confirm",
            key=idempotency_key,
            body=body.model_dump(mode="json", by_alias=True),
            command=command,
        )

    return router
