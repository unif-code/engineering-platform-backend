import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response

from control_plane.app.modules.identity import (
    AccountConflict,
    AccountNotFound,
    AccountStatus,
    InvalidAccountTransition,
    LastEffectiveSuperAdmin,
    Principal,
    StaleAccountVersion,
    create_account,
    get_account,
    issue_temp_password,
    list_accounts,
    record_account_governance_denial,
    reset_totp,
    set_account_status,
)
from control_plane.app.modules.identity.api.admin_dto import (
    AccountCredentialReceiptDto,
    AccountListResponseDto,
    AccountReasonRequestDto,
    AccountSummaryResponseDto,
    CreateAccountRequestDto,
)
from control_plane.app.modules.identity.api.auth_routes import (
    IdentityHttpRuntime,
    _execute,
    _problem,
)
from control_plane.app.shared.api.concurrency import entity_tag, require_if_match
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import PROBLEM_RESPONSES, SERVICE_UNAVAILABLE_RESPONSE
from control_plane.app.shared.idempotency import IdempotentResponse
from control_plane.app.shared.security import assert_same_origin

ACCOUNT_MANAGE_CAPABILITY = "identity.account.manage"
_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)
_ETAG_HEADER = {
    "ETag": {
        "description": "Strong account version ETag for the next write",
        "schema": {"type": "string"},
    }
}


def _write_responses(status: int, description: str) -> dict[int | str, dict[str, Any]]:
    return {
        **_RESPONSES,
        status: {
            "description": description,
            "headers": _ETAG_HEADER,
        },
    }


@dataclass(frozen=True, slots=True)
class _CreatePreflight:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _VersionedPreflight:
    idempotency_key: str
    expected_version: int


def _required_raw_header(request: Request, name: str) -> str:
    value = request.headers.get(name)
    if value is None:
        raise HTTPException(status_code=422, detail=f"Missing {name}")
    return value


def _assert_create_preflight(request: Request) -> None:
    assert_same_origin(request)
    require_idempotency_key(_required_raw_header(request, "Idempotency-Key"))


def _assert_versioned_preflight(request: Request) -> None:
    _assert_create_preflight(request)
    require_if_match(_required_raw_header(request, "If-Match"))


def _create_preflight(
    request: Request,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> _CreatePreflight:
    assert_same_origin(request)
    return _CreatePreflight(idempotency_key)


def _versioned_preflight(
    request: Request,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    expected_version: Annotated[int, Depends(require_if_match)],
) -> _VersionedPreflight:
    assert_same_origin(request)
    return _VersionedPreflight(idempotency_key, expected_version)


def _unimplemented() -> None:
    raise HTTPException(status_code=503, detail="Account governance unavailable")


def _actor(principal: Any) -> Principal:
    employee_no = getattr(principal, "employee_no", None) or getattr(
        principal,
        "employee_id",
        None,
    )
    display_name = getattr(principal, "display_name", None) or getattr(
        principal,
        "name",
        None,
    )
    if not isinstance(employee_no, str) or not isinstance(display_name, str):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return Principal(employee_id=employee_no, name=display_name)


def _actor_id(principal: Any) -> str:
    account_id = getattr(principal, "account_id", None)
    if not isinstance(account_id, str) or not account_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return account_id


def _encode_cursor(employee_no: str, account_id: str) -> str:
    raw = json.dumps([employee_no, account_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    if cursor is None:
        return None, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(part, str) and part for part in value)
            or len(value[0]) != 8
            or not value[0].isascii()
            or not value[0].isdigit()
        ):
            raise ValueError
        UUID(value[1])
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="Invalid cursor") from None
    return value[0], value[1]


def _governance_denial(error: Exception) -> IdempotentResponse:
    if isinstance(error, AccountNotFound):
        return _problem(404, "Account not found")
    if isinstance(error, StaleAccountVersion):
        return _problem(409, "Stale account version")
    if isinstance(error, LastEffectiveSuperAdmin):
        return _problem(409, "Last effective Super Admin required")
    return _problem(409, "Invalid account transition")


def create_admin_account_router(
    runtime_provider: Callable[[], IdentityHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/accounts", tags=["identity"])

    @router.get(
        "",
        operation_id="accounts_list",
        response_model=AccountListResponseDto,
        responses=_RESPONSES,
    )
    def accounts_list(
        principal: Annotated[Any, Depends(principal_provider)],
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> AccountListResponseDto:
        capability_guard(principal, ACCOUNT_MANAGE_CAPABILITY, None)
        runtime = runtime_provider()
        after_employee_no, after_id = _decode_cursor(cursor)
        with runtime.engine.connect() as db:
            values = list_accounts(
                db,
                after_employee_no=after_employee_no,
                after_id=after_id,
                limit=limit + 1,
                dependencies=runtime.dependencies,
            )
        page = values[:limit]
        next_cursor = None
        if len(values) > limit:
            last = page[-1]
            next_cursor = _encode_cursor(last.employee_no, last.id)
        return AccountListResponseDto(
            items=[AccountSummaryResponseDto.from_domain(value) for value in page],
            next_cursor=next_cursor,
        )

    @router.post(
        "",
        operation_id="create",
        status_code=201,
        response_model=AccountCredentialReceiptDto,
        responses=_write_responses(201, "Account created"),
        dependencies=[Depends(_assert_create_preflight), Depends(_create_preflight)],
    )
    def create(
        body: CreateAccountRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_CreatePreflight, Depends(_create_preflight)],
    ) -> Response:
        capability_guard(principal, ACCOUNT_MANAGE_CAPABILITY, None)
        runtime = runtime_provider()
        actor = _actor(principal)

        def command(db: Any) -> IdempotentResponse:
            try:
                account, temporary_password = create_account(
                    db,
                    employee_no=body.employee_no,
                    display_name=body.display_name,
                    profession=body.profession,
                    actor=actor,
                    reason=body.reason,
                    dependencies=runtime.dependencies,
                )
            except AccountConflict:
                return _problem(409, "Account already exists")
            receipt = AccountCredentialReceiptDto(
                account=AccountSummaryResponseDto.from_domain(account),
                temporary_password=temporary_password,
            )
            return IdempotentResponse(
                status_code=201,
                body=receipt.model_dump(mode="json", by_alias=True),
                headers={"ETag": entity_tag(account.version)},
            )

        return _execute(
            runtime,
            actor=_actor_id(principal),
            operation="create",
            method="POST",
            path=request.url.path,
            key=preflight.idempotency_key,
            body=body.model_dump(mode="json", by_alias=True),
            command=command,
        )

    def versioned_route(
        body: AccountReasonRequestDto,
        account_id: Annotated[str, Path(alias="id")],
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        capability_guard(principal, ACCOUNT_MANAGE_CAPABILITY, None)
        runtime = runtime_provider()
        actor = _actor(principal)
        operation = request.scope["route"].operation_id
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedVersion": preflight.expected_version,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                if operation == "reset_password":
                    temporary_password = issue_temp_password(
                        db,
                        account_id=account_id,
                        actor=actor,
                        reason=body.reason,
                        expected_version=preflight.expected_version,
                        dependencies=runtime.dependencies,
                    )
                    account = get_account(
                        db,
                        account_id=account_id,
                        dependencies=runtime.dependencies,
                    )
                    receipt = AccountCredentialReceiptDto(
                        account=AccountSummaryResponseDto.from_domain(account),
                        temporary_password=temporary_password,
                    )
                    return IdempotentResponse(
                        status_code=200,
                        body=receipt.model_dump(mode="json", by_alias=True),
                        headers={"ETag": entity_tag(account.version)},
                    )
                if operation == "totp_reset":
                    account = reset_totp(
                        db,
                        account_id=account_id,
                        expected_version=preflight.expected_version,
                        actor=actor,
                        reason=body.reason,
                        dependencies=runtime.dependencies,
                    )
                else:
                    target_status = (
                        AccountStatus.ENABLED if operation == "enable" else AccountStatus.DISABLED
                    )
                    account = set_account_status(
                        db,
                        account_id=account_id,
                        status=target_status,
                        expected_version=preflight.expected_version,
                        actor=actor,
                        reason=body.reason,
                        dependencies=runtime.dependencies,
                    )
            except (
                AccountNotFound,
                InvalidAccountTransition,
                LastEffectiveSuperAdmin,
                StaleAccountVersion,
            ) as error:
                record_account_governance_denial(
                    db,
                    account_id=account_id,
                    operation=operation,
                    actor=actor,
                    dependencies=runtime.dependencies,
                )
                return _governance_denial(error)
            return IdempotentResponse(
                status_code=204,
                body={},
                headers={"ETag": entity_tag(account.version)},
            )

        return _execute(
            runtime,
            actor=_actor_id(principal),
            operation=operation,
            method="POST",
            path=request.url.path,
            key=preflight.idempotency_key,
            body=body_data,
            command=command,
        )

    for path, operation_id, response_model in (
        ("/{id}/reset-password", "reset_password", AccountCredentialReceiptDto),
        ("/{id}/enable", "enable", None),
        ("/{id}/disable", "disable", None),
        ("/{id}/totp-reset", "totp_reset", None),
    ):
        router.add_api_route(
            path,
            versioned_route,
            methods=["POST"],
            operation_id=operation_id,
            response_model=response_model,
            status_code=200 if response_model is not None else 204,
            responses=_write_responses(
                200 if response_model is not None else 204,
                (
                    "Account password reset"
                    if response_model is not None
                    else "Account governance change applied"
                ),
            ),
            dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
        )

    return router
