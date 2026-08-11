from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from control_plane.app.modules.identity import Principal
from control_plane.app.modules.organization import (
    CorruptStructure,
    InvalidParticipant,
    InvalidStructure,
    OrganizationDependencies,
    get_tree,
    set_superior,
)
from control_plane.app.modules.organization.api.dto import (
    OrgTreeResponseDto,
    SetSuperiorRequestDto,
)
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import PROBLEM_RESPONSES, problem_response
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from control_plane.app.shared.security import assert_same_origin

ORG_TREE_CAPABILITY = "platform.organization.read"
ORG_SET_SUPERIOR_CAPABILITY = "platform.organization.manage"
_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {status: PROBLEM_RESPONSES[status] for status in (401, 403, 409, 422, 500)},
)


@dataclass(frozen=True, slots=True)
class OrganizationHttpRuntime:
    engine: Engine
    dependencies: OrganizationDependencies


def _render(response: IdempotentResponse) -> Response:
    if response.is_problem:
        semantic = dict(response.body)
        title = str(semantic.pop("title"))
        semantic.pop("status", None)
        detail_value = semantic.pop("detail", None)
        detail = str(detail_value) if detail_value is not None else None
        return problem_response(
            response.status_code,
            title,
            detail=detail,
            extra=semantic,
            headers=response.headers,
        )
    if response.status_code == 204:
        return Response(status_code=204, headers=response.headers)
    return JSONResponse(
        status_code=response.status_code,
        content=response.body,
        headers=response.headers,
    )


def create_organization_router(
    runtime_provider: Callable[[], OrganizationHttpRuntime],
    principal_provider: Callable[[], Principal],
    capability_guard: Callable[[Principal, str], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["organization"])

    @router.get(
        "/organization/tree",
        operation_id="org_tree",
        response_model=OrgTreeResponseDto,
        responses=_RESPONSES,
    )
    def organization_tree(
        principal: Annotated[Principal, Depends(principal_provider)],
    ) -> OrgTreeResponseDto:
        capability_guard(principal, ORG_TREE_CAPABILITY)
        runtime = runtime_provider()
        with runtime.engine.connect() as db:
            tree = get_tree(db, dependencies=runtime.dependencies)
        return OrgTreeResponseDto.from_domain(tree)

    @router.put(
        "/accounts/{accountId}/superior",
        operation_id="org_set_superior",
        status_code=204,
        response_model=None,
        responses=_RESPONSES,
    )
    def organization_set_superior(
        account_id: Annotated[str, Path(alias="accountId")],
        body: SetSuperiorRequestDto,
        request: Request,
        principal: Annotated[Principal, Depends(principal_provider)],
        idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    ) -> Response:
        assert_same_origin(request)
        capability_guard(principal, ORG_SET_SUPERIOR_CAPABILITY)
        runtime = runtime_provider()
        body_data = body.model_dump(mode="json", by_alias=True)
        material = runtime.dependencies.secret_manager.load()
        fingerprint = canonical_request_fingerprint(
            operation="org_set_superior",
            method="PUT",
            path=request.url.path,
            body=body_data,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
        try:
            with runtime.engine.begin() as db:
                execution = execute_idempotent(
                    runtime.dependencies.repository_factory(db),
                    actor=principal.employee_id,
                    operation="org_set_superior",
                    key=idempotency_key,
                    fingerprint=fingerprint,
                    command=lambda: _set_superior_response(
                        db,
                        account_id=account_id,
                        body=body,
                        principal=principal,
                        runtime=runtime,
                    ),
                    now=runtime.dependencies.clock.now,
                    new_id=runtime.dependencies.random.uuid4,
                    idempotency_sealing_key=material.idempotency_sealing_key,
                )
        except IdempotencyConflict:
            return problem_response(409, "Idempotency conflict")
        except IdempotencyReplayUnavailable:
            return problem_response(409, "Idempotency replay unavailable")
        return _render(execution.response)

    return router


def _set_superior_response(
    db: Any,
    *,
    account_id: str,
    body: SetSuperiorRequestDto,
    principal: Principal,
    runtime: OrganizationHttpRuntime,
) -> IdempotentResponse:
    try:
        set_superior(
            db,
            account_id=account_id,
            superior_id=body.superior_id,
            actor=principal,
            reason=body.reason,
            dependencies=runtime.dependencies,
        )
    except (InvalidParticipant, InvalidStructure, CorruptStructure):
        return IdempotentResponse(
            status_code=409,
            body={"title": "Organization structure conflict", "status": 409},
            is_problem=True,
        )
    return IdempotentResponse(status_code=204, body={})
