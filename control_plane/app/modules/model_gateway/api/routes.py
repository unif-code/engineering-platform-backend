from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.authorization import AuthorizationPrincipal
from control_plane.app.modules.model_gateway import (
    CatalogDependencies,
    CatalogError,
    CreateDeployment,
    Deployment,
    DeploymentState,
    ModelDeploymentCatalog,
    PatchDeployment,
)
from control_plane.app.modules.model_gateway.adapters import SqlAlchemyDeploymentRepository
from control_plane.app.modules.model_gateway.api.dto import (
    ArchiveModelDeploymentRequestDto,
    CreateModelDeploymentRequestDto,
    ModelDeploymentDto,
    ModelDeploymentsResponseDto,
    PatchModelDeploymentRequestDto,
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
from control_plane.app.shared.security import SecretManagerPort, assert_same_origin

_READ = "platform.model.read"
_MANAGE = "platform.model.manage"
_PROBLEMS = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)
_ETAG = {
    "ETag": {"description": 'Strong candidate revision, e.g. "v1"', "schema": {"type": "string"}}
}
_ENTITY_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **_PROBLEMS,
        200: {"description": "Candidate declaration", "headers": _ETAG},
    },
)
_CREATE_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **_PROBLEMS,
        201: {"description": "Unverified candidate registered", "headers": _ETAG},
    },
)


@dataclass(frozen=True)
class ModelGatewayHttpRuntime:
    engine: Engine
    dependencies: CatalogDependencies
    secret_manager: SecretManagerPort


def _dto(value: Deployment) -> ModelDeploymentDto:
    return ModelDeploymentDto.model_validate(value.model_dump())


def _entity(value: Deployment, status: int) -> IdempotentResponse:
    return IdempotentResponse(
        status_code=status,
        body=_dto(value).model_dump(mode="json", by_alias=True),
        headers={"ETag": entity_tag(value.revision)},
    )


def _denial(error: CatalogError) -> IdempotentResponse:
    status = 409
    if error.code == "MODEL_DEPLOYMENT_NOT_FOUND":
        status = 404
    elif error.code == "INVALID_MODEL_DEPLOYMENT_CURSOR":
        status = 422
    return IdempotentResponse(
        status_code=status,
        is_problem=True,
        body={"title": error.code.replace("_", " ").title(), "code": error.code},
    )


def _render(value: IdempotentResponse) -> Response:
    if value.is_problem:
        return problem_response(
            value.status_code, str(value.body["title"]), extra={"code": value.body["code"]}
        )
    return JSONResponse(value.body, status_code=value.status_code, headers=value.headers)


def _write_preflight(request: Request) -> None:
    assert_same_origin(request)
    key = request.headers.get("Idempotency-Key")
    if key is None:
        raise HTTPException(422, "Missing Idempotency-Key")
    require_idempotency_key(key)


def _versioned_preflight(request: Request) -> None:
    _write_preflight(request)
    value = request.headers.get("If-Match")
    if value is None:
        raise HTTPException(422, "Missing If-Match")
    require_if_match(value)


def _execute(
    runtime: ModelGatewayHttpRuntime,
    *,
    actor: str,
    request: Request,
    key: str,
    operation: str,
    body: dict[str, Any],
    expected_revision: int | None,
    command: Callable[[ModelDeploymentCatalog], Deployment],
    status: int,
) -> Response:
    try:
        material = runtime.secret_manager.load()
        fingerprint = canonical_request_fingerprint(
            operation=operation,
            method=request.method,
            path=request.url.path,
            body={"payload": body, "expectedRevision": expected_revision},
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
        with runtime.engine.begin() as db:
            repository = SqlAlchemyDeploymentRepository(db)
            catalog = ModelDeploymentCatalog(repository, runtime.dependencies)

            def run() -> IdempotentResponse:
                try:
                    return _entity(command(catalog), status)
                except CatalogError as error:
                    return _denial(error)

            result = execute_idempotent(
                repository,
                actor=actor,
                operation=operation,
                key=key,
                fingerprint=fingerprint,
                command=run,
                now=runtime.dependencies.now,
                new_id=runtime.dependencies.new_id,
                idempotency_sealing_key=material.idempotency_sealing_key,
            )
        return _render(result.response)
    except IdempotencyConflict:
        return problem_response(409, "Idempotency conflict", extra={"code": "IDEMPOTENCY_CONFLICT"})
    except IdempotencyReplayUnavailable:
        return problem_response(
            409, "Idempotency replay unavailable", extra={"code": "IDEMPOTENCY_REPLAY_UNAVAILABLE"}
        )
    except SQLAlchemyError:
        return problem_response(503, "Model deployment catalog unavailable")


def create_model_gateway_router(
    runtime_provider: Callable[[], ModelGatewayHttpRuntime],
    principal_dependency: Callable[..., Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/model-deployments", tags=["model-deployments"])

    def readable(
        principal: Annotated[Any, Depends(principal_dependency)],
    ) -> AuthorizationPrincipal:
        capability_guard(principal, _READ, None)
        return cast(AuthorizationPrincipal, principal)

    def writable(
        principal: Annotated[Any, Depends(principal_dependency)],
    ) -> AuthorizationPrincipal:
        capability_guard(principal, _MANAGE, None)
        if not principal.is_super_admin:
            raise HTTPException(403, "Current Super Admin required")
        return cast(AuthorizationPrincipal, principal)

    @router.get(
        "",
        operation_id="model_deployments_list",
        response_model=ModelDeploymentsResponseDto,
        responses=_PROBLEMS,
    )
    def list_deployments(
        principal: Annotated[AuthorizationPrincipal, Depends(readable)],
        state: Annotated[DeploymentState | None, Query()] = None,
        q: Annotated[
            str,
            Query(
                max_length=120,
                description="Literal case-insensitive name or deployment key substring",
            ),
        ] = "",
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                items, next_cursor = ModelDeploymentCatalog(
                    SqlAlchemyDeploymentRepository(db),
                    runtime.dependencies,
                ).list(state=state, query=q.strip(), cursor=cursor, page_size=page_size)
            return JSONResponse(
                ModelDeploymentsResponseDto(
                    items=[_dto(item) for item in items],
                    next_cursor=next_cursor,
                ).model_dump(mode="json", by_alias=True)
            )
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Model deployment catalog unavailable")

    @router.get(
        "/{deploymentId}",
        operation_id="model_deployments_get",
        response_model=ModelDeploymentDto,
        responses=_ENTITY_RESPONSES,
    )
    def get_deployment(
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        principal: Annotated[AuthorizationPrincipal, Depends(readable)],
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                value = ModelDeploymentCatalog(
                    SqlAlchemyDeploymentRepository(db), runtime.dependencies
                ).get(str(deployment_id))
            return _render(_entity(value, 200))
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Model deployment catalog unavailable")

    @router.post(
        "",
        operation_id="model_deployments_create",
        response_model=ModelDeploymentDto,
        status_code=201,
        responses=_CREATE_RESPONSES,
        dependencies=[Depends(_write_preflight)],
    )
    def create_deployment(
        request: Request,
        body: CreateModelDeploymentRequestDto,
        principal: Annotated[AuthorizationPrincipal, Depends(writable)],
        key: Annotated[str, Depends(require_idempotency_key)],
    ) -> Response:
        value = CreateDeployment.model_validate(body.model_dump())
        return _execute(
            runtime_provider(),
            actor=principal.account_id,
            request=request,
            key=key,
            operation="model_deployment.create",
            body=body.model_dump(mode="json"),
            expected_revision=None,
            command=lambda catalog: catalog.create(value, actor=principal.account_id),
            status=201,
        )

    @router.patch(
        "/{deploymentId}",
        operation_id="model_deployments_patch",
        response_model=ModelDeploymentDto,
        responses=_ENTITY_RESPONSES,
        dependencies=[Depends(_versioned_preflight)],
    )
    def patch_deployment(
        request: Request,
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        body: PatchModelDeploymentRequestDto,
        principal: Annotated[AuthorizationPrincipal, Depends(writable)],
        key: Annotated[str, Depends(require_idempotency_key)],
        expected_revision: Annotated[int, Depends(require_if_match)],
    ) -> Response:
        payload = body.model_dump(mode="json", exclude_unset=True)
        value = PatchDeployment.model_validate(payload)
        return _execute(
            runtime_provider(),
            actor=principal.account_id,
            request=request,
            key=key,
            operation="model_deployment.patch",
            body=payload,
            expected_revision=expected_revision,
            command=lambda catalog: catalog.patch(
                str(deployment_id),
                value,
                expected_revision=expected_revision,
                actor=principal.account_id,
            ),
            status=200,
        )

    @router.post(
        "/{deploymentId}:archive",
        operation_id="model_deployments_archive",
        response_model=ModelDeploymentDto,
        responses=_ENTITY_RESPONSES,
        dependencies=[Depends(_versioned_preflight)],
    )
    def archive_deployment(
        request: Request,
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        body: ArchiveModelDeploymentRequestDto,
        principal: Annotated[AuthorizationPrincipal, Depends(writable)],
        key: Annotated[str, Depends(require_idempotency_key)],
        expected_revision: Annotated[int, Depends(require_if_match)],
    ) -> Response:
        return _execute(
            runtime_provider(),
            actor=principal.account_id,
            request=request,
            key=key,
            operation="model_deployment.archive",
            body=body.model_dump(mode="json"),
            expected_revision=expected_revision,
            command=lambda catalog: catalog.archive(
                str(deployment_id),
                body.reason,
                expected_revision=expected_revision,
                actor=principal.account_id,
            ),
            status=200,
        )

    return router
