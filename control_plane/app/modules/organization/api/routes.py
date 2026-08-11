from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text

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
from control_plane.app.modules.organization.ports import SecurityChangePort
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

ORG_TREE_CAPABILITY = "platform.organization.read"
ORG_SET_SUPERIOR_CAPABILITY = "platform.organization.manage"
_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)


@dataclass(frozen=True, slots=True)
class OrganizationHttpRuntime:
    engine: Engine
    dependencies: OrganizationDependencies
    security_changes: SecurityChangePort | None = None


@dataclass(frozen=True, slots=True)
class _WritePreflight:
    idempotency_key: str


def _actor_id(principal: Any) -> str:
    value = getattr(principal, "account_id", None)
    if isinstance(value, str) and value:
        return value
    legacy = getattr(principal, "employee_id", None)
    if isinstance(legacy, str) and legacy:
        return legacy
    raise ValueError("organization principal requires a stable identifier")


def _required_raw_header(request: Request, name: str) -> str:
    value = request.headers.get(name)
    if value is None:
        raise HTTPException(status_code=422, detail=f"Missing {name}")
    return value


def _assert_write_preflight(request: Request) -> None:
    assert_same_origin(request)
    require_idempotency_key(_required_raw_header(request, "Idempotency-Key"))


def _write_preflight(
    request: Request,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> _WritePreflight:
    assert_same_origin(request)
    return _WritePreflight(idempotency_key=idempotency_key)


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
    capability_guard: Callable[[Principal, str, str | None], None],
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
        capability_guard(principal, ORG_TREE_CAPABILITY, None)
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
        dependencies=[Depends(_assert_write_preflight), Depends(_write_preflight)],
    )
    def organization_set_superior(
        account_id: Annotated[str, Path(alias="accountId")],
        body: SetSuperiorRequestDto,
        request: Request,
        principal: Annotated[Principal, Depends(principal_provider)],
        preflight: Annotated[_WritePreflight, Depends(_write_preflight)],
    ) -> Response:
        capability_guard(principal, ORG_SET_SUPERIOR_CAPABILITY, None)
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
        security_changes = runtime.security_changes
        ticket = None
        response: Response
        try:
            with runtime.engine.begin() as db:
                if security_changes is not None:
                    source_transaction_id = str(
                        db.execute(text("SELECT pg_current_xact_id()")).scalar_one()
                    )
                    ticket = security_changes.begin(
                        reason="organization structure change",
                        source_module="organization",
                        actor=_actor_id(principal),
                        operation="org_set_superior",
                        idempotency_key=preflight.idempotency_key,
                        source_transaction_id=source_transaction_id,
                        affected_account_ids=(account_id,),
                        recompute_membership=True,
                    )
                execution = execute_idempotent(
                    runtime.dependencies.repository_factory(db),
                    actor=_actor_id(principal),
                    operation="org_set_superior",
                    key=preflight.idempotency_key,
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
            response = problem_response(409, "Idempotency conflict")
        except IdempotencyReplayUnavailable:
            response = problem_response(409, "Idempotency replay unavailable")
        except Exception:
            if ticket is not None:
                assert security_changes is not None
                security_changes.cancel(ticket)
            raise
        else:
            response = _render(execution.response)
        if ticket is not None:
            assert security_changes is not None
            if 200 <= response.status_code < 300:
                try:
                    security_changes.complete(ticket)
                except Exception:
                    return problem_response(503, "Authorization convergence unavailable")
            else:
                security_changes.cancel(ticket)
        return response

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
