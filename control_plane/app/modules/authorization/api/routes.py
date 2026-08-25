from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse

from control_plane.app.modules.authorization import (
    AuthorizationPrincipal,
    GrantNotFound,
    InvalidGrant,
    Scope,
    ScopeType,
    StaleGrantVersion,
    grant,
    list_grants,
    revoke,
)
from control_plane.app.modules.authorization.api.dependencies import (
    current_principal,
    require_capability,
)
from control_plane.app.modules.authorization.api.dto import (
    GrantCreateRequestDto,
    GrantListResponseDto,
    GrantResponseDto,
    GrantRevokeRequestDto,
    NavigationItemDto,
    PrincipalDto,
)
from control_plane.app.modules.authorization.api.runtime import (
    AuthorizationHttpRuntime as AuthorizationHttpRuntime,
)
from control_plane.app.shared.api.concurrency import entity_tag, require_if_match
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    problem_response,
)
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from control_plane.app.shared.security import assert_same_origin

AUTHORIZATION_MANAGE_CAPABILITY = "platform.authorization.manage"
_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)


@dataclass(frozen=True, slots=True)
class _CreateWritePreflight:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _VersionedWritePreflight:
    idempotency_key: str
    expected_version: int


def _required_raw_header(request: Request, name: str) -> str:
    value = request.headers.get(name)
    if value is None:
        raise HTTPException(status_code=422, detail=f"Missing {name}")
    return value


def _assert_create_write_preflight(request: Request) -> None:
    assert_same_origin(request)
    require_idempotency_key(_required_raw_header(request, "Idempotency-Key"))


def _assert_versioned_write_preflight(request: Request) -> None:
    _assert_create_write_preflight(request)
    require_if_match(_required_raw_header(request, "If-Match"))


def _create_write_preflight(
    request: Request,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> _CreateWritePreflight:
    assert_same_origin(request)
    return _CreateWritePreflight(idempotency_key=idempotency_key)


def _versioned_write_preflight(
    request: Request,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    expected_version: Annotated[int, Depends(require_if_match)],
) -> _VersionedWritePreflight:
    assert_same_origin(request)
    return _VersionedWritePreflight(
        idempotency_key=idempotency_key,
        expected_version=expected_version,
    )


def _render(response: IdempotentResponse) -> Response:
    if response.is_problem:
        semantic = dict(response.body)
        title = str(semantic.pop("title"))
        semantic.pop("status", None)
        detail_value = semantic.pop("detail", None)
        return problem_response(
            response.status_code,
            title,
            detail=str(detail_value) if detail_value is not None else None,
            extra=semantic,
            headers=response.headers,
        )
    return JSONResponse(
        status_code=response.status_code,
        content=response.body,
        headers=response.headers,
    )


def _grant_response(value: Any, *, status_code: int) -> IdempotentResponse:
    dto = GrantResponseDto.from_domain(value)
    return IdempotentResponse(
        status_code=status_code,
        body=dto.model_dump(mode="json", by_alias=True),
        headers={"ETag": entity_tag(value.version)},
    )


def _grant_denial(error: Exception) -> IdempotentResponse:
    if isinstance(error, GrantNotFound):
        status_code, title = 404, "Grant not found"
    elif isinstance(error, StaleGrantVersion):
        status_code, title = 409, "Stale grant version"
    else:
        status_code, title = 409, "Invalid grant"
    return IdempotentResponse(
        status_code=status_code,
        body={"title": title, "status": status_code},
        is_problem=True,
    )


def _execute(
    runtime: AuthorizationHttpRuntime,
    *,
    principal: AuthorizationPrincipal,
    operation: str,
    key: str,
    method: str,
    path: str,
    body: dict[str, object],
    command: Callable[[Any], IdempotentResponse],
) -> Response:
    material = runtime.dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=operation,
        method=method,
        path=path,
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    try:
        with runtime.engine.begin() as db:
            repository = runtime.dependencies.repository_factory(db)
            execution = execute_idempotent(
                repository,
                actor=principal.account_id,
                operation=operation,
                key=key,
                fingerprint=fingerprint,
                command=lambda: command(db),
                now=runtime.dependencies.clock.now,
                new_id=runtime.dependencies.random.uuid4,
                idempotency_sealing_key=material.idempotency_sealing_key,
            )
    except IdempotencyConflict:
        return problem_response(409, "Idempotency conflict")
    except IdempotencyReplayUnavailable:
        return problem_response(409, "Idempotency replay unavailable")
    return _render(execution.response)


def create_authorization_router(
    runtime_provider: Callable[[], AuthorizationHttpRuntime],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["authorization"])
    principal_dependency = current_principal(runtime_provider)
    manage_dependency = require_capability(
        AUTHORIZATION_MANAGE_CAPABILITY,
        runtime_provider=runtime_provider,
    )

    @router.get(
        "/me",
        operation_id="identity_me",
        response_model=PrincipalDto,
        responses=_RESPONSES,
    )
    def me(
        principal: Annotated[AuthorizationPrincipal, Depends(principal_dependency)],
    ) -> PrincipalDto | Response:
        runtime = runtime_provider()
        try:
            organization = runtime.organization_summary(principal.account_id)
            workspaces = runtime.workspace_summaries(principal.account_id)
        except Exception:
            return problem_response(503, "Authorization unavailable")
        return PrincipalDto.from_domain(
            principal,
            organization=organization,
            workspaces=workspaces,
        )

    @router.get(
        "/navigation",
        operation_id="identity_navigation",
        response_model=list[NavigationItemDto],
        responses=_RESPONSES,
    )
    def navigation(
        principal: Annotated[AuthorizationPrincipal, Depends(principal_dependency)],
    ) -> list[NavigationItemDto] | Response:
        runtime = runtime_provider()
        effective_routes = {
            (item.capability, item.scope.scope_type.value) for item in principal.capabilities
        }
        try:
            with runtime.engine.connect() as db:
                rows = runtime.dependencies.repository_factory(db).route_registry()
        except Exception:
            return problem_response(503, "Authorization unavailable")
        result: list[NavigationItemDto] = []
        for row in rows:
            key = (str(row["capability"]), str(row["scope_type"]))
            if key not in effective_routes:
                continue
            meta = dict(row["meta"])
            result.append(
                NavigationItemDto(
                    route_key=str(row["route_key"]),
                    name=str(meta["name"]),
                    order=int(meta["order"]),
                    capability=str(row["capability"]),
                    scope_type=ScopeType(row["scope_type"]),
                    meta=meta,
                )
            )
        return result

    @router.get(
        "/admin/grants",
        operation_id="grants_list",
        response_model=GrantListResponseDto,
        responses=_RESPONSES,
    )
    def grants_list(
        principal: Annotated[AuthorizationPrincipal, manage_dependency],
    ) -> GrantListResponseDto:
        runtime = runtime_provider()
        with runtime.engine.connect() as db:
            values = list_grants(db, dependencies=runtime.dependencies)
        return GrantListResponseDto(items=[GrantResponseDto.from_domain(value) for value in values])

    @router.post(
        "/admin/grants",
        operation_id="grants_create",
        status_code=201,
        response_model=GrantResponseDto,
        responses=_RESPONSES,
        dependencies=[
            Depends(_assert_create_write_preflight),
            Depends(_create_write_preflight),
        ],
    )
    def grants_create(
        body: GrantCreateRequestDto,
        request: Request,
        principal: Annotated[AuthorizationPrincipal, manage_dependency],
        preflight: Annotated[_CreateWritePreflight, Depends(_create_write_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        body_data = body.model_dump(mode="json", by_alias=True)

        def command(db: Any) -> IdempotentResponse:
            try:
                created = grant(
                    db,
                    principal_id=body.principal_id,
                    capability=body.capability,
                    scope=Scope(scope_type=body.scope_type, scope_id=body.scope_id),
                    source=body.source,
                    valid_from=body.valid_from,
                    valid_to=body.valid_to,
                    actor=principal,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except (InvalidGrant, ValueError) as error:
                return _grant_denial(error)
            return _grant_response(created, status_code=201)

        return _execute(
            runtime,
            principal=principal,
            operation="grants_create",
            key=preflight.idempotency_key,
            method="POST",
            path=request.url.path,
            body=body_data,
            command=command,
        )

    @router.delete(
        "/admin/grants/{id}",
        operation_id="grants_revoke",
        response_model=GrantResponseDto,
        responses=_RESPONSES,
        dependencies=[
            Depends(_assert_versioned_write_preflight),
            Depends(_versioned_write_preflight),
        ],
    )
    def grants_revoke(
        grant_id: Annotated[str, Path(alias="id")],
        body: GrantRevokeRequestDto,
        request: Request,
        principal: Annotated[AuthorizationPrincipal, manage_dependency],
        preflight: Annotated[_VersionedWritePreflight, Depends(_versioned_write_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": preflight.expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                revoked = revoke(
                    db,
                    grant_id=grant_id,
                    expected_version=preflight.expected_version,
                    actor=principal,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except (GrantNotFound, StaleGrantVersion, InvalidGrant) as error:
                return _grant_denial(error)
            return _grant_response(revoked, status_code=200)

        return _execute(
            runtime,
            principal=principal,
            operation="grants_revoke",
            key=preflight.idempotency_key,
            method="DELETE",
            path=request.url.path,
            body=body_data,
            command=command,
        )

    return router
