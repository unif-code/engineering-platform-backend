from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import Message, Receive, Scope, Send

from control_plane.app.modules.authorization import AuthorizationPrincipal
from control_plane.app.modules.model_gateway import (
    CatalogError,
    CreateValidationDossier,
    ModelValidationDossiers,
)
from control_plane.app.modules.model_gateway.adapters.dossiers import (
    SqlAlchemyValidationDossierRepository,
)
from control_plane.app.modules.model_gateway.api.dto import (
    CreateValidationDossierRequestDto,
    ValidationDossierDetailDto,
    ValidationDossierListDto,
    ValidationDossierReceiptDto,
)
from control_plane.app.modules.model_gateway.api.routes import (
    ModelGatewayHttpRuntime,
    _denial,
    _render,
    _versioned_preflight,
)
from control_plane.app.modules.model_gateway.domain.dossiers import MAX_DOSSIER_BODY_BYTES
from control_plane.app.shared.api.concurrency import require_if_match
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    Problem,
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
        413: {"model": Problem, "description": "Dossier request exceeds 65536 bytes"},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)


class DossierRoute(APIRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if scope["method"] == "POST" and message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_DOSSIER_BODY_BYTES:
                    raise HTTPException(413, "Dossier request exceeds 65536 bytes")
            return message

        await super().handle(scope, bounded_receive, send)


async def _submitted_body(request: Request) -> dict[str, Any]:
    # The typed DTO validates/normalizes saved content; idempotency also binds
    # discarded query/fragment and original declarations, without storing them.
    return cast(dict[str, Any], await request.json())


def create_model_dossier_router(
    runtime_provider: Callable[[], ModelGatewayHttpRuntime],
    principal_dependency: Callable[..., Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/model-deployments/{deploymentId}/validation-dossiers",
        tags=["model-validation-dossiers"],
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
        operation_id="model_validation_dossiers_create",
        status_code=201,
        response_model=ValidationDossierReceiptDto,
        responses=_PROBLEMS,
        dependencies=[Depends(_versioned_preflight)],
    )
    def create_dossier(
        request: Request,
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        body: CreateValidationDossierRequestDto,
        submitted_body: Annotated[dict[str, Any], Depends(_submitted_body)],
        principal: Annotated[AuthorizationPrincipal, Depends(manager)],
        key: Annotated[str, Depends(require_idempotency_key)],
        expected_revision: Annotated[int, Depends(require_if_match)],
    ) -> Response:
        runtime = runtime_provider()
        material = runtime.secret_manager.load()
        fingerprint = canonical_request_fingerprint(
            operation="model_validation_dossier.create",
            method="POST",
            path=request.url.path,
            body={"body": submitted_body, "candidateRevision": expected_revision},
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
        try:
            with runtime.engine.begin() as db:
                repository = SqlAlchemyValidationDossierRepository(db)
                dossiers = ModelValidationDossiers(
                    repository, runtime.dependencies, runtime.connections
                )

                def command() -> IdempotentResponse:
                    try:
                        value = dossiers.create(
                            str(deployment_id),
                            CreateValidationDossier.model_validate(body.model_dump()),
                            expected_revision=expected_revision,
                            actor=principal.account_id,
                        )
                        return IdempotentResponse(
                            status_code=201,
                            body=ValidationDossierReceiptDto.from_dossier(value).model_dump(
                                mode="json", by_alias=True
                            ),
                        )
                    except CatalogError as error:
                        return _denial(error)

                execution = execute_idempotent(
                    repository,
                    actor=principal.account_id,
                    operation="model_validation_dossier.create",
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
            return problem_response(503, "Model validation dossiers unavailable")

    @router.get(
        "",
        operation_id="model_validation_dossiers_list",
        response_model=ValidationDossierListDto,
        responses=_PROBLEMS,
    )
    def list_dossiers(
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        principal: Annotated[AuthorizationPrincipal, Depends(reader)],
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                dossiers = ModelValidationDossiers(
                    SqlAlchemyValidationDossierRepository(db),
                    runtime.dependencies,
                    runtime.connections,
                )
                items, next_cursor = dossiers.list(
                    str(deployment_id), cursor=cursor, page_size=page_size
                )
            return JSONResponse(
                ValidationDossierListDto(
                    items=[ValidationDossierReceiptDto.from_dossier(item) for item in items],
                    next_cursor=next_cursor,
                ).model_dump(mode="json", by_alias=True)
            )
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Model validation dossiers unavailable")

    @router.get(
        "/{dossierId}",
        operation_id="model_validation_dossiers_get",
        response_model=ValidationDossierDetailDto,
        responses={
            **_PROBLEMS,
            200: {
                "headers": {
                    "ETag": {
                        "schema": {"type": "string"},
                        "description": (
                            "Opaque detail ETag includes currentness/expiration; "
                            "not snapshotHash or candidate If-Match."
                        ),
                    }
                }
            },
        },
    )
    def get_dossier(
        deployment_id: Annotated[UUID, Path(alias="deploymentId")],
        dossier_id: Annotated[UUID, Path(alias="dossierId")],
        principal: Annotated[AuthorizationPrincipal, Depends(reader)],
    ) -> Response:
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                dossiers = ModelValidationDossiers(
                    SqlAlchemyValidationDossierRepository(db),
                    runtime.dependencies,
                    runtime.connections,
                )
                snapshot = dossiers.get(str(deployment_id), str(dossier_id))
                projection = dossiers.project(snapshot, now=runtime.dependencies.now())
                value = ValidationDossierDetailDto.model_validate(projection.model_dump())
            return JSONResponse(
                value.model_dump(mode="json", by_alias=True),
                headers={"ETag": dossiers.etag(projection), "Cache-Control": "no-store"},
            )
        except CatalogError as error:
            return _render(_denial(error))
        except SQLAlchemyError:
            return problem_response(503, "Model validation dossiers unavailable")

    return router
