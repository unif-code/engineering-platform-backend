from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from control_plane.app.modules.authorization import PLATFORM_SUPER_ADMIN_MANAGE
from control_plane.app.modules.identity import (
    AccountNotFound,
    LastEffectiveSuperAdmin,
    StaleAccountVersion,
    SuperAdminConflict,
    SuperAdminPermissionDenied,
    TotpChallengeFailed,
    add_super_admin,
    issue_super_admin_challenge,
    list_super_admins,
    remove_super_admin,
)
from control_plane.app.modules.identity.api.auth_routes import (
    IdentityHttpRuntime,
    _execute,
    _problem,
)
from control_plane.app.modules.identity.api.super_admin_dto import (
    AddSuperAdminRequestDto,
    RemoveSuperAdminRequestDto,
    SuperAdminListResponseDto,
    SuperAdminResponseDto,
)
from control_plane.app.modules.identity.application.idempotency import IdempotentResponse
from control_plane.app.shared.api.concurrency import entity_tag, require_if_match
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
)
from control_plane.app.shared.security import assert_same_origin

_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)
_WRITE_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **_RESPONSES,
        200: {
            "description": "Super Admin lifecycle change applied",
            "headers": {
                "ETag": {
                    "description": "Strong account version ETag for the next write",
                    "schema": {"type": "string"},
                }
            },
        },
    },
)


@dataclass(frozen=True, slots=True)
class _WritePreflight:
    idempotency_key: str
    expected_version: int


def _required_raw_header(request: Request, name: str) -> str:
    value = request.headers.get(name)
    if value is None:
        raise HTTPException(status_code=422, detail=f"Missing {name}")
    return value


def _assert_write_preflight(request: Request) -> None:
    assert_same_origin(request)
    require_idempotency_key(_required_raw_header(request, "Idempotency-Key"))
    require_if_match(_required_raw_header(request, "If-Match"))


def _write_preflight(
    request: Request,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    expected_version: Annotated[int, Depends(require_if_match)],
) -> _WritePreflight:
    assert_same_origin(request)
    return _WritePreflight(idempotency_key, expected_version)


def _account_id(principal: Any) -> str:
    account_id = getattr(principal, "account_id", None)
    if not isinstance(account_id, str) or not account_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return account_id


def _success(account: Any) -> IdempotentResponse:
    dto = SuperAdminResponseDto.from_domain(account)
    return IdempotentResponse(
        status_code=200,
        body=dto.model_dump(mode="json", by_alias=True),
        headers={"ETag": entity_tag(account.version)},
    )


def _denial(error: Exception) -> IdempotentResponse:
    if isinstance(error, AccountNotFound):
        return _problem(404, "Account not found")
    if isinstance(error, (SuperAdminPermissionDenied, TotpChallengeFailed)):
        return _problem(403, "Super Admin verification failed")
    if isinstance(error, StaleAccountVersion):
        return _problem(409, "Stale account version")
    if isinstance(error, LastEffectiveSuperAdmin):
        return _problem(409, "Last effective Super Admin required")
    return _problem(409, "Super Admin governance conflict")


def create_super_admin_router(
    runtime_provider: Callable[[], IdentityHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["identity"])

    @router.get(
        "/super-admins",
        operation_id="super_admin_list",
        response_model=SuperAdminListResponseDto,
        responses=_RESPONSES,
    )
    def super_admin_list(
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> SuperAdminListResponseDto:
        capability_guard(principal, PLATFORM_SUPER_ADMIN_MANAGE, None)
        runtime = runtime_provider()
        with runtime.engine.connect() as db:
            accounts = list_super_admins(db, dependencies=runtime.dependencies)
        return SuperAdminListResponseDto(
            items=[SuperAdminResponseDto.from_domain(value) for value in accounts]
        )

    @router.post(
        "/super-admins",
        operation_id="super_admin_add",
        response_model=SuperAdminResponseDto,
        responses=_WRITE_RESPONSES,
        dependencies=[Depends(_assert_write_preflight), Depends(_write_preflight)],
    )
    def super_admin_add(
        body: AddSuperAdminRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_WritePreflight, Depends(_write_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_SUPER_ADMIN_MANAGE, None)
        runtime = runtime_provider()
        actor_account_id = _account_id(principal)
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": preflight.expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                challenge = issue_super_admin_challenge(
                    db,
                    actor_account_id=actor_account_id,
                    operation="ADD",
                    dependencies=runtime.dependencies,
                )
                account = add_super_admin(
                    db,
                    target_account_id=body.account_id,
                    actor_account_id=actor_account_id,
                    challenge_token=challenge,
                    totp_code=body.totp_code,
                    reason=body.reason,
                    expected_version=preflight.expected_version,
                    dependencies=runtime.dependencies,
                )
            except (
                AccountNotFound,
                LastEffectiveSuperAdmin,
                StaleAccountVersion,
                SuperAdminConflict,
                SuperAdminPermissionDenied,
                TotpChallengeFailed,
            ) as error:
                return _denial(error)
            return _success(account)

        return _execute(
            runtime,
            actor=actor_account_id,
            operation="super_admin_add",
            method="POST",
            path=request.url.path,
            key=preflight.idempotency_key,
            body=body_data,
            command=command,
        )

    @router.delete(
        "/super-admins/{id}",
        operation_id="super_admin_remove",
        response_model=SuperAdminResponseDto,
        responses=_WRITE_RESPONSES,
        dependencies=[Depends(_assert_write_preflight), Depends(_write_preflight)],
    )
    def super_admin_remove(
        account_id: Annotated[str, Path(alias="id")],
        body: RemoveSuperAdminRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_WritePreflight, Depends(_write_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_SUPER_ADMIN_MANAGE, None)
        runtime = runtime_provider()
        actor_account_id = _account_id(principal)
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": preflight.expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                challenge = issue_super_admin_challenge(
                    db,
                    actor_account_id=actor_account_id,
                    operation="REMOVE",
                    dependencies=runtime.dependencies,
                )
                account = remove_super_admin(
                    db,
                    target_account_id=account_id,
                    actor_account_id=actor_account_id,
                    challenge_token=challenge,
                    totp_code=body.totp_code,
                    reason=body.reason,
                    expected_version=preflight.expected_version,
                    dependencies=runtime.dependencies,
                )
            except (
                AccountNotFound,
                LastEffectiveSuperAdmin,
                StaleAccountVersion,
                SuperAdminConflict,
                SuperAdminPermissionDenied,
                TotpChallengeFailed,
            ) as error:
                return _denial(error)
            return _success(account)

        return _execute(
            runtime,
            actor=actor_account_id,
            operation="super_admin_remove",
            method="DELETE",
            path=request.url.path,
            key=preflight.idempotency_key,
            body=body_data,
            command=command,
        )

    return router
