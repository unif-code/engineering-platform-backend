from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from control_plane.app.modules.identity import Principal
from control_plane.app.modules.workspace import (
    WorkspaceDependencies,
    WorkspaceError,
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
from control_plane.app.shared.api.concurrency import entity_tag, require_if_match
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

WORKSPACE_READ_CAPABILITY = "platform.workspace.read"
WORKSPACE_MANAGE_CAPABILITY = "platform.workspace.manage"
_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
)


@dataclass(frozen=True, slots=True)
class WorkspaceHttpRuntime:
    engine: Engine
    dependencies: WorkspaceDependencies


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


def _denial() -> IdempotentResponse:
    return IdempotentResponse(
        status_code=409,
        body={"title": "Workspace governance conflict", "status": 409},
        is_problem=True,
    )


def _execute(
    runtime: WorkspaceHttpRuntime,
    *,
    principal: Principal,
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
            execution = execute_idempotent(
                runtime.dependencies.repository_factory(db),
                actor=principal.employee_id,
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


def create_workspace_router(
    runtime_provider: Callable[[], WorkspaceHttpRuntime],
    principal_provider: Callable[[], Principal],
    capability_guard: Callable[[Principal, str], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["workspace"])

    @router.get(
        "/workspaces",
        operation_id="workspace_list",
        response_model=WorkspaceListResponseDto,
        responses=_RESPONSES,
    )
    def workspace_list(
        principal: Annotated[Principal, Depends(principal_provider)],
    ) -> WorkspaceListResponseDto:
        capability_guard(principal, WORKSPACE_READ_CAPABILITY)
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
    )
    def workspace_create(
        body: CreateWorkspaceRequestDto,
        request: Request,
        principal: Annotated[Principal, Depends(principal_provider)],
        idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    ) -> Response:
        assert_same_origin(request)
        capability_guard(principal, WORKSPACE_MANAGE_CAPABILITY)
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
            except WorkspaceError:
                return _denial()
            return _workspace_response(created, status_code=201)

        return _execute(
            runtime,
            principal=principal,
            operation="workspace_create",
            key=idempotency_key,
            method="POST",
            path=request.url.path,
            body=body_data,
            command=command,
        )

    @router.post(
        "/workspaces/{id}/leaders",
        operation_id="workspace_invite_leader",
        response_model=WorkspaceResponseDto,
        responses=_RESPONSES,
    )
    def workspace_invite_leader(
        workspace_id: Annotated[str, Path(alias="id")],
        body: InviteLeaderRequestDto,
        request: Request,
        principal: Annotated[Principal, Depends(principal_provider)],
        idempotency_key: Annotated[str, Depends(require_idempotency_key)],
        expected_version: Annotated[int, Depends(require_if_match)],
    ) -> Response:
        assert_same_origin(request)
        capability_guard(principal, WORKSPACE_MANAGE_CAPABILITY)
        runtime = runtime_provider()
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                updated = invite_leader(
                    db,
                    workspace_id=workspace_id,
                    account_id=body.account_id,
                    expected_version=expected_version,
                    actor=principal,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except WorkspaceError:
                return _denial()
            return _workspace_response(updated, status_code=200)

        return _execute(
            runtime,
            principal=principal,
            operation="workspace_invite_leader",
            key=idempotency_key,
            method="POST",
            path=request.url.path,
            body=body_data,
            command=command,
        )

    @router.delete(
        "/workspaces/{id}/leaders/{accountId}",
        operation_id="workspace_remove_leader",
        response_model=WorkspaceResponseDto,
        responses=_RESPONSES,
    )
    def workspace_remove_leader(
        workspace_id: Annotated[str, Path(alias="id")],
        account_id: Annotated[str, Path(alias="accountId")],
        body: RemoveLeaderRequestDto,
        request: Request,
        principal: Annotated[Principal, Depends(principal_provider)],
        idempotency_key: Annotated[str, Depends(require_idempotency_key)],
        expected_version: Annotated[int, Depends(require_if_match)],
    ) -> Response:
        assert_same_origin(request)
        capability_guard(principal, WORKSPACE_MANAGE_CAPABILITY)
        runtime = runtime_provider()
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                updated = remove_leader(
                    db,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    expected_version=expected_version,
                    actor=principal,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except WorkspaceError:
                return _denial()
            return _workspace_response(updated, status_code=200)

        return _execute(
            runtime,
            principal=principal,
            operation="workspace_remove_leader",
            key=idempotency_key,
            method="DELETE",
            path=request.url.path,
            body=body_data,
            command=command,
        )

    @router.post(
        "/workspaces/{id}/transfer-owner",
        operation_id="workspace_transfer_owner",
        response_model=WorkspaceResponseDto,
        responses=_RESPONSES,
    )
    def workspace_transfer_owner(
        workspace_id: Annotated[str, Path(alias="id")],
        body: TransferOwnerRequestDto,
        request: Request,
        principal: Annotated[Principal, Depends(principal_provider)],
        idempotency_key: Annotated[str, Depends(require_idempotency_key)],
        expected_version: Annotated[int, Depends(require_if_match)],
    ) -> Response:
        assert_same_origin(request)
        capability_guard(principal, WORKSPACE_MANAGE_CAPABILITY)
        runtime = runtime_provider()
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                updated = transfer_owner(
                    db,
                    workspace_id=workspace_id,
                    new_owner_id=body.new_owner_id,
                    expected_version=expected_version,
                    actor=principal,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except WorkspaceError:
                return _denial()
            return _workspace_response(updated, status_code=200)

        return _execute(
            runtime,
            principal=principal,
            operation="workspace_transfer_owner",
            key=idempotency_key,
            method="POST",
            path=request.url.path,
            body=body_data,
            command=command,
        )

    @router.get(
        "/workspaces/{id}/members",
        operation_id="workspace_members",
        response_model=FormalMemberListResponseDto,
        responses=_RESPONSES,
    )
    def workspace_members(
        workspace_id: Annotated[str, Path(alias="id")],
        principal: Annotated[Principal, Depends(principal_provider)],
    ) -> FormalMemberListResponseDto:
        capability_guard(principal, WORKSPACE_READ_CAPABILITY)
        runtime = runtime_provider()
        with runtime.engine.connect() as db:
            values = members(
                db,
                workspace_id=workspace_id,
                dependencies=runtime.dependencies,
            )
        return FormalMemberListResponseDto(
            items=[FormalMemberResponseDto.from_domain(value) for value in values]
        )

    return router
