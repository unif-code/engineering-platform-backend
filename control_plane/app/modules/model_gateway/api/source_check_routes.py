from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.authorization import AuthorizationPrincipal
from control_plane.app.modules.model_gateway import CatalogError
from control_plane.app.modules.model_gateway.adapters.source_checks import (
    SqlAlchemyMaterialSourceCheckRepository,
)
from control_plane.app.modules.model_gateway.api.dossier_routes import _PROBLEMS, DossierRoute
from control_plane.app.modules.model_gateway.api.dto import (
    CreateMaterialSourceCheckRequestDto,
    MaterialSourceCheckDetailDto,
    MaterialSourceCheckListDto,
    MaterialSourceCheckReceiptDto,
)
from control_plane.app.modules.model_gateway.api.routes import (
    ModelGatewayHttpRuntime,
    _denial,
    _render,
    _versioned_preflight,
)
from control_plane.app.modules.model_gateway.application.source_checks import (
    ModelMaterialSourceChecks,
)
from control_plane.app.shared.api.concurrency import require_if_match
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import problem_response
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)


def create_model_source_check_router(
    runtime_provider: Callable[[], ModelGatewayHttpRuntime],
    principal_dependency: Callable[..., Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/model-deployments/{deploymentId}/validation-dossiers/{dossierId}/source-checks",
        tags=["model-material-source-checks"],
        route_class=DossierRoute,
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
        operation_id="model_material_source_checks_create",
        status_code=201,
        response_model=MaterialSourceCheckReceiptDto,
        responses=_PROBLEMS,
        dependencies=[Depends(_versioned_preflight)],
    )
    def create_source_check(
        request: Request,
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        dossier_id: Annotated[UUID, Path(alias="dossierId")],
        body: CreateMaterialSourceCheckRequestDto,
        principal: Annotated[AuthorizationPrincipal, Depends(manager)],
        key: Annotated[str, Depends(require_idempotency_key)],
        expected_revision: Annotated[int, Depends(require_if_match)],
    ) -> Response:
        runtime = runtime_provider()
        material = runtime.secret_manager.load()
        fingerprint = canonical_request_fingerprint(
            operation="model_material_source_check.create",
            method="POST",
            path=request.url.path,
            body={"body": body.model_dump(), "candidateRevision": expected_revision},
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
        try:
            with runtime.engine.begin() as db:
                repository = SqlAlchemyMaterialSourceCheckRepository(db)
                checks = ModelMaterialSourceChecks(
                    repository, runtime.dependencies, runtime.material_sources
                )

                def command() -> IdempotentResponse:
                    try:
                        value = checks.create(
                            str(deployment_id),
                            str(dossier_id),
                            material_index=body.material_index,
                            expected_revision=expected_revision,
                            actor=principal.account_id,
                        )
                        return IdempotentResponse(
                            status_code=201,
                            body=MaterialSourceCheckReceiptDto.from_check(value).model_dump(
                                mode="json", by_alias=True
                            ),
                        )
                    except CatalogError as error:
                        return _denial(error)

                result = execute_idempotent(
                    repository,
                    actor=principal.account_id,
                    operation="model_material_source_check.create",
                    key=key,
                    fingerprint=fingerprint,
                    command=command,
                    now=runtime.dependencies.now,
                    new_id=runtime.dependencies.new_id,
                    idempotency_sealing_key=material.idempotency_sealing_key,
                )
            return _render(result.response)
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
            return problem_response(503, "Material source checks unavailable")

    @router.get(
        "",
        operation_id="model_material_source_checks_list",
        response_model=MaterialSourceCheckListDto,
        responses=_PROBLEMS,
    )
    def list_source_checks(
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        dossier_id: Annotated[UUID, Path(alias="dossierId")],
        principal: Annotated[AuthorizationPrincipal, Depends(reader)],
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                checks = ModelMaterialSourceChecks(
                    SqlAlchemyMaterialSourceCheckRepository(db),
                    runtime.dependencies,
                    runtime.material_sources,
                )
                items, next_cursor = checks.list(
                    str(deployment_id), str(dossier_id), cursor=cursor, page_size=page_size
                )
            return JSONResponse(
                MaterialSourceCheckListDto(
                    items=[MaterialSourceCheckReceiptDto.from_check(item) for item in items],
                    next_cursor=next_cursor,
                ).model_dump(mode="json", by_alias=True)
            )
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Material source checks unavailable")

    @router.get(
        "/{sourceCheckId}",
        operation_id="model_material_source_checks_get",
        response_model=MaterialSourceCheckDetailDto,
        responses={
            **_PROBLEMS,
            200: {
                "headers": {
                    "ETag": {
                        "schema": {"type": "string"},
                        "description": (
                            "Opaque current projection ETag; distinct from check/dossier "
                            "snapshot hashes and candidate If-Match."
                        ),
                    }
                }
            },
        },
    )
    def get_source_check(
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        dossier_id: Annotated[UUID, Path(alias="dossierId")],
        source_check_id: Annotated[UUID, Path(alias="sourceCheckId")],
        principal: Annotated[AuthorizationPrincipal, Depends(reader)],
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                checks = ModelMaterialSourceChecks(
                    SqlAlchemyMaterialSourceCheckRepository(db),
                    runtime.dependencies,
                    runtime.material_sources,
                )
                value = checks.get(str(deployment_id), str(dossier_id), str(source_check_id))
                projection = checks.project(value)
                dto = MaterialSourceCheckDetailDto.model_validate(projection.model_dump())
            return JSONResponse(
                dto.model_dump(mode="json", by_alias=True),
                headers={"ETag": checks.etag(projection), "Cache-Control": "no-store"},
            )
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Material source checks unavailable")

    return router
