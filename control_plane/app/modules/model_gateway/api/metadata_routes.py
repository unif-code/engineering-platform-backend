from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.authorization import AuthorizationPrincipal
from control_plane.app.modules.model_gateway.adapters.metadata_observations import (
    SqlAlchemyMetadataRepository,
)
from control_plane.app.modules.model_gateway.api.metadata_dto import (
    CreateMetadataObservationRequestDto,
    MetadataHistoryItemDto,
    MetadataObservationDto,
    MetadataObservationListDto,
    MetadataObservationReceiptDto,
)
from control_plane.app.modules.model_gateway.api.routes import (
    ModelGatewayHttpRuntime,
    _denial,
    _render,
    _versioned_preflight,
)
from control_plane.app.modules.model_gateway.application.metadata import (
    ModelMetadataObservations,
    metadata_admission,
    metadata_applicability,
)
from control_plane.app.modules.model_gateway.domain import CatalogError
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
        **{s: PROBLEM_RESPONSES[s] for s in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)
_READ_RESPONSES: dict[int | str, dict[str, Any]] = _PROBLEMS | {
    200: {
        "headers": {
            "ETag": {
                "schema": {"type": "string"},
                "description": "Opaque projection ETag; never use for candidate If-Match.",
            }
        }
    },
}


def _observations(
    runtime: ModelGatewayHttpRuntime, repository: SqlAlchemyMetadataRepository
) -> ModelMetadataObservations:
    return ModelMetadataObservations(
        repository, runtime.dependencies, runtime.connections, runtime.metadata_sources
    )


def create_model_metadata_router(
    runtime_provider: Callable[[], ModelGatewayHttpRuntime],
    principal_dependency: Callable[..., Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/model-deployments/{deploymentId}/metadata-observations",
        tags=["model-metadata-observations"],
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
        operation_id="model_metadata_observations_create",
        response_model=MetadataObservationReceiptDto,
        status_code=202,
        responses=_PROBLEMS,
        dependencies=[Depends(_versioned_preflight)],
    )
    def create_observation(
        request: Request,
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        body: CreateMetadataObservationRequestDto,
        principal: Annotated[AuthorizationPrincipal, Depends(manager)],
        key: Annotated[str, Depends(require_idempotency_key)],
        expected_revision: Annotated[int, Depends(require_if_match)],
    ) -> Response:
        runtime = runtime_provider()
        try:
            material = runtime.secret_manager.load()
            fingerprint = canonical_request_fingerprint(
                operation="model_metadata_observation.create",
                method="POST",
                path=request.url.path,
                body={"body": body.model_dump(), "candidateRevision": expected_revision},
                idempotency_sealing_key=material.idempotency_sealing_key,
            )
            with runtime.engine.begin() as db:
                repository = SqlAlchemyMetadataRepository(db)
                observations = _observations(runtime, repository)

                def command() -> IdempotentResponse:
                    try:
                        value = observations.accept(
                            str(deployment_id),
                            expected_revision=expected_revision,
                            source_fingerprint=body.source_fingerprint,
                            actor=principal.account_id,
                        )
                        return IdempotentResponse(
                            status_code=202,
                            body=MetadataObservationReceiptDto.from_observation(value).model_dump(
                                mode="json", by_alias=True
                            ),
                        )
                    except CatalogError as error:
                        return _denial(error)

                execution = execute_idempotent(
                    repository,
                    actor=principal.account_id,
                    operation="model_metadata_observation.create",
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
            return problem_response(503, "Idempotency replay unavailable")
        except SQLAlchemyError:
            return problem_response(503, "Model metadata observations unavailable")

    @router.get(
        "",
        operation_id="model_metadata_observations_list",
        response_model=MetadataObservationListDto,
        responses=_READ_RESPONSES,
    )
    def list_observations(
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        principal: Annotated[AuthorizationPrincipal, Depends(reader)],
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                observations = _observations(runtime, SqlAlchemyMetadataRepository(db))
                candidate = observations.deployment(str(deployment_id))
                items, next_cursor = observations.list(
                    str(deployment_id), cursor=cursor, page_size=page_size
                )
                data = MetadataObservationListDto(
                    items=[MetadataHistoryItemDto.from_observation(x) for x in items],
                    next_cursor=next_cursor,
                    candidate_revision=candidate.revision,
                    admission=metadata_admission(
                        candidate, runtime.connections, runtime.metadata_sources
                    ),
                ).model_dump(mode="json", by_alias=True)
            return JSONResponse(
                data,
                headers={"ETag": f'"metadata-list-{digest(data)}"', "Cache-Control": "no-store"},
            )
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Model metadata observations unavailable")

    @router.get(
        "/{observationId}",
        operation_id="model_metadata_observations_get",
        response_model=MetadataObservationDto,
        responses=_READ_RESPONSES,
    )
    def get_observation(
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        observation_id: Annotated[UUID, Path(alias="observationId")],
        principal: Annotated[AuthorizationPrincipal, Depends(reader)],
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                observations = _observations(runtime, SqlAlchemyMetadataRepository(db))
                candidate = observations.deployment(str(deployment_id))
                value = observations.get(str(deployment_id), str(observation_id))
                data = MetadataObservationDto.from_observation(
                    value,
                    *metadata_applicability(
                        value,
                        candidate,
                        runtime.connections,
                        runtime.metadata_sources,
                    ),
                ).model_dump(mode="json", by_alias=True)
            return JSONResponse(
                data, headers={"ETag": f'"metadata-{digest(data)}"', "Cache-Control": "no-store"}
            )
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Model metadata observations unavailable")

    return router
