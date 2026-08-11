from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text

from control_plane.app.modules.identity import SessionPrincipal
from control_plane.app.modules.workspace import (
    StaleWorkspaceVersion,
    WorkspaceArchived,
    WorkspaceDependencies,
    WorkspaceError,
    WorkspaceNotFound,
    WorkspaceOwnerRequired,
    create_workspace,
    invite_leader,
    list_workspaces,
    members,
    remove_leader,
    transfer_owner,
)
from control_plane.app.modules.workspace.api.dto import (
    CreateWorkspaceRequestDto,
    FormalMemberListResponseDto,
    FormalMemberResponseDto,
    InviteLeaderRequestDto,
    RemoveLeaderRequestDto,
    TransferOwnerRequestDto,
    WorkspaceListResponseDto,
    WorkspaceResponseDto,
)
from control_plane.app.modules.workspace.ports import SecurityChangePort
from control_plane.app.shared.api.concurrency import entity_tag, require_if_match
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    problem_response,
)
from control_plane.app.shared.idempotency import (
    IdempotencyClaim,
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from control_plane.app.shared.security import assert_same_origin

WORKSPACE_READ_CAPABILITY = "platform.workspace.read"
WORKSPACE_MANAGE_CAPABILITY = "platform.workspace.manage"
_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)


@dataclass(frozen=True, slots=True)
class WorkspaceHttpRuntime:
    engine: Engine
    dependencies: WorkspaceDependencies
    security_changes: SecurityChangePort | None = None


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
        detail = str(detail_value) if detail_value is not None else None
        return problem_response(
            response.status_code,
            title,
            detail=detail,
            extra=semantic,
            headers=response.headers,
        )
    return JSONResponse(
        status_code=response.status_code,
        content=response.body,
        headers=response.headers,
    )


def _workspace_response(workspace: Any, *, status_code: int) -> IdempotentResponse:
    dto = WorkspaceResponseDto.from_domain(workspace)
    return IdempotentResponse(
        status_code=status_code,
        body=dto.model_dump(mode="json", by_alias=True),
        headers={"ETag": entity_tag(workspace.version)},
    )


def _denial(error: WorkspaceError) -> IdempotentResponse:
    if isinstance(error, WorkspaceNotFound):
        status_code = 404
        title = "Workspace not found"
    elif isinstance(error, WorkspaceOwnerRequired):
        status_code = 403
        title = "Workspace owner required"
    elif isinstance(error, StaleWorkspaceVersion):
        status_code = 409
        title = "Stale workspace version"
    elif isinstance(error, WorkspaceArchived):
        status_code = 409
        title = "Workspace archived"
    else:
        status_code = 409
        title = "Workspace governance conflict"
    return IdempotentResponse(
        status_code=status_code,
        body={"title": title, "status": status_code},
        is_problem=True,
    )


def _execute(
    runtime: WorkspaceHttpRuntime,
    *,
    principal: SessionPrincipal,
    operation: str,
    key: str,
    method: str,
    path: str,
    body: dict[str, object],
    command: Callable[[Any], IdempotentResponse],
    affected_account_ids: tuple[str, ...] = (),
    affected_workspace_ids: tuple[str, ...] = (),
) -> Response:
    material = runtime.dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=operation,
        method=method,
        path=path,
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    security_changes = runtime.security_changes
    ticket = None
    response: Response
    try:
        with runtime.engine.begin() as db:
            source_transaction_id = str(
                db.execute(text("SELECT pg_current_xact_id()")).scalar_one()
            )

            def register_security_change(claim: IdempotencyClaim) -> None:
                nonlocal ticket
                if security_changes is None:
                    return
                ticket = security_changes.begin(
                    reason=f"{operation} security change",
                    source_module="workspace",
                    actor=principal.account_id,
                    operation=operation,
                    idempotency_key=key,
                    request_fingerprint=claim.request_fingerprint,
                    idempotency_claim_id=claim.record_id,
                    source_transaction_id=source_transaction_id,
                    affected_account_ids=affected_account_ids,
                    affected_workspace_ids=affected_workspace_ids,
                )

            execution = execute_idempotent(
                runtime.dependencies.repository_factory(db),
                actor=principal.account_id,
                operation=operation,
                key=key,
                fingerprint=fingerprint,
                command=lambda: command(db),
                now=runtime.dependencies.clock.now,
                new_id=runtime.dependencies.random.uuid4,
                idempotency_sealing_key=material.idempotency_sealing_key,
                on_claimed=register_security_change,
            )
            if ticket is not None and not (200 <= execution.response.status_code < 300):
                assert security_changes is not None
                security_changes.cancel(ticket)
                ticket = None
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
        if (
            execution.replayed
            and 200 <= response.status_code < 300
            and security_changes is not None
            and not security_changes.claim_converged("workspace", execution.claim.record_id)
        ):
            return problem_response(503, "Authorization convergence unavailable")
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


def create_workspace_router(
    runtime_provider: Callable[[], WorkspaceHttpRuntime],
    principal_provider: Callable[[], SessionPrincipal],
    capability_guard: Callable[[SessionPrincipal, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["workspace"])

    @router.get(
        "/workspaces",
        operation_id="workspace_list",
        response_model=WorkspaceListResponseDto,
        responses=_RESPONSES,
    )
    def workspace_list(
        principal: Annotated[SessionPrincipal, Depends(principal_provider)],
    ) -> WorkspaceListResponseDto:
        capability_guard(principal, WORKSPACE_READ_CAPABILITY, None)
        runtime = runtime_provider()
        with runtime.engine.connect() as db:
            values = list_workspaces(db, dependencies=runtime.dependencies)
        return WorkspaceListResponseDto(
            items=[WorkspaceResponseDto.from_domain(value) for value in values]
        )

    @router.post(
        "/workspaces",
        operation_id="workspace_create",
        status_code=201,
        response_model=WorkspaceResponseDto,
        responses=_RESPONSES,
        dependencies=[
            Depends(_assert_create_write_preflight),
            Depends(_create_write_preflight),
        ],
    )
    def workspace_create(
        body: CreateWorkspaceRequestDto,
        request: Request,
        principal: Annotated[SessionPrincipal, Depends(principal_provider)],
        preflight: Annotated[_CreateWritePreflight, Depends(_create_write_preflight)],
    ) -> Response:
        capability_guard(principal, WORKSPACE_MANAGE_CAPABILITY, None)
        runtime = runtime_provider()
        body_data = body.model_dump(mode="json", by_alias=True)

        def command(db: Any) -> IdempotentResponse:
            try:
                created = create_workspace(
                    db,
                    name=body.name,
                    owner_id=body.owner_id,
                    actor=principal,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except WorkspaceError as error:
                return _denial(error)
            return _workspace_response(created, status_code=201)

        return _execute(
            runtime,
            principal=principal,
            operation="workspace_create",
            key=preflight.idempotency_key,
            method="POST",
            path=request.url.path,
            body=body_data,
            command=command,
            affected_account_ids=(body.owner_id,),
        )

    @router.post(
        "/workspaces/{id}/leaders",
        operation_id="workspace_invite_leader",
        response_model=WorkspaceResponseDto,
        responses=_RESPONSES,
        dependencies=[
            Depends(_assert_versioned_write_preflight),
            Depends(_versioned_write_preflight),
        ],
    )
    def workspace_invite_leader(
        workspace_id: Annotated[str, Path(alias="id")],
        body: InviteLeaderRequestDto,
        request: Request,
        principal: Annotated[SessionPrincipal, Depends(principal_provider)],
        preflight: Annotated[_VersionedWritePreflight, Depends(_versioned_write_preflight)],
    ) -> Response:
        capability_guard(principal, WORKSPACE_MANAGE_CAPABILITY, workspace_id)
        runtime = runtime_provider()
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": preflight.expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                updated = invite_leader(
                    db,
                    workspace_id=workspace_id,
                    account_id=body.account_id,
                    expected_version=preflight.expected_version,
                    actor=principal,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except WorkspaceError as error:
                return _denial(error)
            return _workspace_response(updated, status_code=200)

        return _execute(
            runtime,
            principal=principal,
            operation="workspace_invite_leader",
            key=preflight.idempotency_key,
            method="POST",
            path=request.url.path,
            body=body_data,
            command=command,
            affected_account_ids=(body.account_id,),
            affected_workspace_ids=(workspace_id,),
        )

    @router.delete(
        "/workspaces/{id}/leaders/{accountId}",
        operation_id="workspace_remove_leader",
        response_model=WorkspaceResponseDto,
        responses=_RESPONSES,
        dependencies=[
            Depends(_assert_versioned_write_preflight),
            Depends(_versioned_write_preflight),
        ],
    )
    def workspace_remove_leader(
        workspace_id: Annotated[str, Path(alias="id")],
        account_id: Annotated[str, Path(alias="accountId")],
        body: RemoveLeaderRequestDto,
        request: Request,
        principal: Annotated[SessionPrincipal, Depends(principal_provider)],
        preflight: Annotated[_VersionedWritePreflight, Depends(_versioned_write_preflight)],
    ) -> Response:
        capability_guard(principal, WORKSPACE_MANAGE_CAPABILITY, workspace_id)
        runtime = runtime_provider()
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": preflight.expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                updated = remove_leader(
                    db,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    expected_version=preflight.expected_version,
                    actor=principal,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except WorkspaceError as error:
                return _denial(error)
            return _workspace_response(updated, status_code=200)

        return _execute(
            runtime,
            principal=principal,
            operation="workspace_remove_leader",
            key=preflight.idempotency_key,
            method="DELETE",
            path=request.url.path,
            body=body_data,
            command=command,
            affected_account_ids=(account_id,),
            affected_workspace_ids=(workspace_id,),
        )

    @router.post(
        "/workspaces/{id}/transfer-owner",
        operation_id="workspace_transfer_owner",
        response_model=WorkspaceResponseDto,
        responses=_RESPONSES,
        dependencies=[
            Depends(_assert_versioned_write_preflight),
            Depends(_versioned_write_preflight),
        ],
    )
    def workspace_transfer_owner(
        workspace_id: Annotated[str, Path(alias="id")],
        body: TransferOwnerRequestDto,
        request: Request,
        principal: Annotated[SessionPrincipal, Depends(principal_provider)],
        preflight: Annotated[_VersionedWritePreflight, Depends(_versioned_write_preflight)],
    ) -> Response:
        capability_guard(principal, WORKSPACE_MANAGE_CAPABILITY, workspace_id)
        runtime = runtime_provider()
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": preflight.expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                updated = transfer_owner(
                    db,
                    workspace_id=workspace_id,
                    new_owner_id=body.new_owner_id,
                    expected_version=preflight.expected_version,
                    actor=principal,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except WorkspaceError as error:
                return _denial(error)
            return _workspace_response(updated, status_code=200)

        return _execute(
            runtime,
            principal=principal,
            operation="workspace_transfer_owner",
            key=preflight.idempotency_key,
            method="POST",
            path=request.url.path,
            body=body_data,
            command=command,
            affected_account_ids=(body.new_owner_id,),
            affected_workspace_ids=(workspace_id,),
        )

    @router.get(
        "/workspaces/{id}/members",
        operation_id="workspace_members",
        response_model=FormalMemberListResponseDto,
        responses=_RESPONSES,
    )
    def workspace_members(
        workspace_id: Annotated[str, Path(alias="id")],
        principal: Annotated[SessionPrincipal, Depends(principal_provider)],
    ) -> FormalMemberListResponseDto | Response:
        capability_guard(principal, WORKSPACE_READ_CAPABILITY, workspace_id)
        runtime = runtime_provider()
        with runtime.engine.connect() as db:
            try:
                values = members(
                    db,
                    workspace_id=workspace_id,
                    dependencies=runtime.dependencies,
                )
            except WorkspaceError as error:
                return _render(_denial(error))
        return FormalMemberListResponseDto(
            items=[FormalMemberResponseDto.from_domain(value) for value in values]
        )

    return router
