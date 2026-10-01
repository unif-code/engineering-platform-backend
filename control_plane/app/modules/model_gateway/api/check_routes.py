from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.authorization import AuthorizationPrincipal
from control_plane.app.modules.model_gateway import CatalogError
from control_plane.app.modules.model_gateway.adapters.checks import SqlAlchemyCheckRepository
from control_plane.app.modules.model_gateway.api.dto import (
    ConnectionCheckDto,
    ConnectionCheckListDto,
    ConnectionCheckReceiptDto,
    CreateConnectionCheckRequestDto,
)
from control_plane.app.modules.model_gateway.api.routes import (
    ModelGatewayHttpRuntime,
    _denial,
    _render,
    _versioned_preflight,
)
from control_plane.app.modules.model_gateway.application.checks import (
    ModelConnectionChecks,
    currentness,
)
from control_plane.app.modules.model_gateway.domain.connections import digest
from control_plane.app.shared.api.concurrency import require_if_match
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

_PROBLEMS = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)
_DETAIL_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **_PROBLEMS,
        200: {
            "headers": {
                "ETag": {
                    "schema": {"type": "string"},
                    "description": (
                        "Opaque strong check ETag, including currentness. "
                        "Never use it for candidate If-Match."
                    ),
                }
            }
        },
    },
)


def create_model_check_router(
    runtime_provider: Callable[[], ModelGatewayHttpRuntime],
    principal_dependency: Callable[..., Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/model-deployments/{deploymentId}/connection-checks",
        tags=["model-connection-checks"],
    )

    def reader(principal: Annotated[Any, Depends(principal_dependency)]) -> AuthorizationPrincipal:
        capability_guard(principal, "platform.model.read", None)
        return cast(AuthorizationPrincipal, principal)

    def manager(principal: Annotated[Any, Depends(principal_dependency)]) -> AuthorizationPrincipal:
        capability_guard(principal, "platform.model.manage", None)
        if not principal.is_super_admin:
            raise HTTPException(403, "Current Super Admin required")
        return cast(AuthorizationPrincipal, principal)

    @router.post(
        "",
        operation_id="model_connection_checks_create",
        response_model=ConnectionCheckReceiptDto,
        status_code=202,
        responses=_PROBLEMS,
        dependencies=[Depends(_versioned_preflight)],
    )
    def create_check(
        request: Request,
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        body: CreateConnectionCheckRequestDto,
        principal: Annotated[AuthorizationPrincipal, Depends(manager)],
        key: Annotated[str, Depends(require_idempotency_key)],
        expected_revision: Annotated[int, Depends(require_if_match)],
    ) -> Response:
        runtime = runtime_provider()
        material = runtime.secret_manager.load()
        fingerprint = canonical_request_fingerprint(
            operation="model_connection_check.create",
            method="POST",
            path=request.url.path,
            body={"body": body.model_dump(), "candidateRevision": expected_revision},
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
        try:
            with runtime.engine.begin() as db:
                repository = SqlAlchemyCheckRepository(db)
                checks = ModelConnectionChecks(
                    repository, runtime.dependencies, runtime.connections
                )

                def command() -> IdempotentResponse:
                    try:
                        check = checks.accept(
                            str(deployment_id),
                            expected_revision=expected_revision,
                            actor=principal.account_id,
                            check_kind=body.check_kind,
                        )
                        return IdempotentResponse(
                            status_code=202,
                            body=ConnectionCheckReceiptDto.from_check(check).model_dump(
                                mode="json", by_alias=True
                            ),
                        )
                    except CatalogError as error:
                        return _denial(error)

                execution = execute_idempotent(
                    repository,
                    actor=principal.account_id,
                    operation="model_connection_check.create",
                    key=key,
                    fingerprint=fingerprint,
                    command=command,
                    now=runtime.dependencies.now,
                    new_id=runtime.dependencies.new_id,
                    idempotency_sealing_key=material.idempotency_sealing_key,
                )
            return _render(execution.response)
        except IdempotencyConflict:
            return problem_response(
                409, "Idempotency conflict", extra={"code": "IDEMPOTENCY_CONFLICT"}
            )
        except IdempotencyReplayUnavailable:
            return problem_response(
                409,
                "Idempotency replay unavailable",
                extra={"code": "IDEMPOTENCY_REPLAY_UNAVAILABLE"},
            )
        except SQLAlchemyError:
            return problem_response(503, "Model connection checks unavailable")

    @router.get(
        "",
        operation_id="model_connection_checks_list",
        response_model=ConnectionCheckListDto,
        responses=_PROBLEMS,
    )
    def list_checks(
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        principal: Annotated[AuthorizationPrincipal, Depends(reader)],
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                checks = ModelConnectionChecks(
                    SqlAlchemyCheckRepository(db), runtime.dependencies, runtime.connections
                )
                deployment = checks.deployment(str(deployment_id))
                items, next_cursor = checks.list(
                    str(deployment_id), cursor=cursor, page_size=page_size
                )
                values = [
                    ConnectionCheckDto.from_check(
                        item, *currentness(item, deployment, runtime.connections)
                    )
                    for item in items
                ]
            return JSONResponse(
                ConnectionCheckListDto(items=values, next_cursor=next_cursor).model_dump(
                    mode="json", by_alias=True
                )
            )
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Model connection checks unavailable")

    @router.get(
        "/{checkId}",
        operation_id="model_connection_checks_get",
        response_model=ConnectionCheckDto,
        responses=_DETAIL_RESPONSES,
    )
    def get_check(
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        check_id: Annotated[UUID, Path(alias="checkId")],
        principal: Annotated[AuthorizationPrincipal, Depends(reader)],
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                checks = ModelConnectionChecks(
                    SqlAlchemyCheckRepository(db), runtime.dependencies, runtime.connections
                )
                deployment = checks.deployment(str(deployment_id))
                check = checks.get(str(deployment_id), str(check_id))
                value = ConnectionCheckDto.from_check(
                    check, *currentness(check, deployment, runtime.connections)
                ).model_dump(mode="json", by_alias=True)
            return JSONResponse(value, headers={"ETag": f'"check-{digest(value)}"'})
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Model connection checks unavailable")

    return router
