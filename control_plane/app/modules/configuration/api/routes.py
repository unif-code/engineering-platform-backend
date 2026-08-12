from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from control_plane.app.modules.authorization import PLATFORM_CONFIGURATION_MANAGE
from control_plane.app.modules.configuration import (
    ConfigurationDependencies,
    ConfigurationError,
    DraftArchived,
    DraftNotFound,
    DraftOwnerRequired,
    InvalidPolicyValue,
    PolicySnapshotUnavailable,
    PolicyVerificationFailed,
    PolicyVersionNotFound,
    SourceStale,
    StaleDraftBase,
    StaleDraftRevision,
    active_snapshot,
    catalog,
    create_draft,
    preview,
    update_draft,
    validate_draft,
)
from control_plane.app.modules.configuration import (
    policy_versions as list_policy_versions,
)
from control_plane.app.modules.configuration.adapters import IdentityPolicyOwner
from control_plane.app.modules.configuration.api.dto import (
    DraftResponseDto,
    DraftValidationResponseDto,
    DraftValuesRequestDto,
    PolicyCatalogResponseDto,
    PolicyKeyDto,
    PolicySnapshotDto,
    PolicyVersionsResponseDto,
    PreviewResponseDto,
    PublishDraftRequestDto,
    PublishedVersionDto,
    RollbackPolicyRequestDto,
    ValidateDraftRequestDto,
)
from control_plane.app.modules.identity import (
    IdentityPolicyCommandRuntime,
    OwnedPolicySnapshotUnavailable,
)
from control_plane.app.shared.api.concurrency import entity_tag, require_if_match
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
from control_plane.app.shared.security import SecretManagerPort, assert_same_origin

_PROBLEMS = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)
_ETAG_HEADER = {
    "ETag": {
        "description": "Strong draft revision ETag for the next write",
        "schema": {"type": "string"},
    }
}
_CREATE_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {**_PROBLEMS, 201: {"description": "Draft created", "headers": _ETAG_HEADER}},
)
_WRITE_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {**_PROBLEMS, 200: {"description": "Draft updated", "headers": _ETAG_HEADER}},
)
_PUBLISH_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {**_PROBLEMS, 201: {"description": "Policy fact created", "headers": _ETAG_HEADER}},
)


@dataclass(frozen=True, slots=True)
class ConfigurationHttpRuntime:
    engine: Engine
    dependencies: ConfigurationDependencies
    secret_manager: SecretManagerPort
    policy_commands: IdentityPolicyCommandRuntime | None = None


@dataclass(frozen=True, slots=True)
class _CreatePreflight:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _VersionedPreflight:
    idempotency_key: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class _RevisionPreflight:
    expected_revision: int


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


def _assert_revision_preflight(request: Request) -> None:
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
    expected_revision: Annotated[int, Depends(require_if_match)],
) -> _VersionedPreflight:
    assert_same_origin(request)
    return _VersionedPreflight(idempotency_key, expected_revision)


def _revision_preflight(
    expected_revision: Annotated[int, Depends(require_if_match)],
) -> _RevisionPreflight:
    return _RevisionPreflight(expected_revision)


def _account_id(principal: Any) -> str:
    account_id = getattr(principal, "account_id", None)
    if not isinstance(account_id, str) or not account_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return account_id


def _problem(error: ConfigurationError) -> IdempotentResponse:
    if isinstance(error, DraftNotFound):
        status, title = 404, "Draft not found"
    elif isinstance(error, DraftOwnerRequired):
        status, title = 403, "Draft owner required"
    elif isinstance(error, InvalidPolicyValue):
        status, title = 422, "Invalid policy value"
    elif isinstance(error, PolicyVerificationFailed):
        return IdempotentResponse(
            status_code=403,
            body={
                "title": "Policy reauthentication failed",
                "status": 403,
                "code": "REAUTHENTICATION_FAILED",
            },
            is_problem=True,
        )
    elif isinstance(error, PolicyVersionNotFound):
        status, title = 404, "Policy version not found"
    elif isinstance(error, SourceStale):
        return IdempotentResponse(
            status_code=409,
            body={"title": "Source policy is stale", "status": 409, "code": "SOURCE_STALE"},
            is_problem=True,
        )
    elif isinstance(error, (StaleDraftRevision, StaleDraftBase)):
        status, title = 409, "Stale draft revision"
    elif isinstance(error, DraftArchived):
        status, title = 409, "Draft archived"
    else:
        status, title = 409, "Configuration conflict"
    return IdempotentResponse(
        status_code=status,
        body={"title": title, "status": status},
        is_problem=True,
    )


def _render(value: IdempotentResponse) -> Response:
    if value.is_problem:
        body = dict(value.body)
        title = str(body.pop("title"))
        body.pop("status", None)
        detail_value = body.pop("detail", None)
        return problem_response(
            value.status_code,
            title,
            detail=None if detail_value is None else str(detail_value),
            extra=body,
            headers=value.headers,
        )
    return JSONResponse(
        status_code=value.status_code,
        content=value.body,
        headers=value.headers,
    )


def _execute(
    runtime: ConfigurationHttpRuntime,
    *,
    actor_id: str,
    operation: str,
    method: str,
    path: str,
    key: str,
    body: dict[str, object],
    command: Callable[[Any], IdempotentResponse],
) -> Response:
    material = runtime.secret_manager.load()
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
                IdentityPolicyOwner(db),
                actor=actor_id,
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
    except PolicySnapshotUnavailable:
        return problem_response(503, "Effective policy unavailable")
    return _render(execution.response)


def _policy_commands(runtime: ConfigurationHttpRuntime) -> IdentityPolicyCommandRuntime:
    if runtime.policy_commands is None:
        raise RuntimeError("Identity policy command runtime is unavailable")
    return runtime.policy_commands


def _execute_policy_command(command: Callable[[], IdempotentResponse]) -> Response:
    try:
        return _render(command())
    except IdempotencyConflict:
        return problem_response(409, "Idempotency conflict")
    except IdempotencyReplayUnavailable:
        return problem_response(409, "Idempotency replay unavailable")
    except OwnedPolicySnapshotUnavailable:
        return problem_response(503, "Effective policy unavailable")


def create_configuration_router(
    runtime_provider: Callable[[], ConfigurationHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["configuration"])

    @router.get(
        "/policies",
        operation_id="policies_catalog",
        response_model=PolicyCatalogResponseDto,
        responses=_PROBLEMS,
    )
    def policies_catalog(
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> PolicyCatalogResponseDto | Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                keys = catalog(db, "identity")
                snapshot = active_snapshot(db, "identity")
        except PolicySnapshotUnavailable:
            return problem_response(503, "Effective policy unavailable")
        return PolicyCatalogResponseDto(
            items=[PolicyKeyDto.from_domain(key) for key in keys],
            active=PolicySnapshotDto.from_domain(snapshot),
        )

    @router.post(
        "/policies/{namespace}/drafts",
        operation_id="draft_create",
        status_code=201,
        response_model=DraftResponseDto,
        responses=_CREATE_RESPONSES,
        dependencies=[Depends(_assert_create_preflight), Depends(_create_preflight)],
    )
    def draft_create(
        namespace: Annotated[str, Path(min_length=1)],
        body: DraftValuesRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_CreatePreflight, Depends(_create_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        actor_id = _account_id(principal)
        body_data = body.model_dump(mode="json", by_alias=True)

        def command(db: Any) -> IdempotentResponse:
            try:
                draft = create_draft(
                    db,
                    namespace=namespace,
                    values=body.values,
                    actor_id=actor_id,
                    dependencies=runtime.dependencies,
                )
            except ConfigurationError as error:
                return _problem(error)
            dto = DraftResponseDto.from_domain(draft)
            return IdempotentResponse(
                status_code=201,
                body=dto.model_dump(mode="json", by_alias=True),
                headers={"ETag": entity_tag(draft.revision)},
            )

        return _execute(
            runtime,
            actor_id=actor_id,
            operation="draft_create",
            method="POST",
            path=request.url.path,
            key=preflight.idempotency_key,
            body=body_data,
            command=command,
        )

    @router.patch(
        "/policies/{namespace}/drafts/{draft_id}",
        operation_id="draft_update",
        response_model=DraftResponseDto,
        responses=_WRITE_RESPONSES,
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def draft_update(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        body: DraftValuesRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        actor_id = _account_id(principal)
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedRevision": preflight.expected_revision,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                draft = update_draft(
                    db,
                    namespace=namespace,
                    draft_id=draft_id,
                    values=body.values,
                    actor_id=actor_id,
                    expected_revision=preflight.expected_revision,
                    dependencies=runtime.dependencies,
                )
            except ConfigurationError as error:
                return _problem(error)
            dto = DraftResponseDto.from_domain(draft)
            return IdempotentResponse(
                status_code=200,
                body=dto.model_dump(mode="json", by_alias=True),
                headers={"ETag": entity_tag(draft.revision)},
            )

        return _execute(
            runtime,
            actor_id=actor_id,
            operation="draft_update",
            method="PATCH",
            path=request.url.path,
            key=preflight.idempotency_key,
            body=body_data,
            command=command,
        )

    @router.post(
        "/policies/{namespace}/drafts/{draft_id}/validate",
        operation_id="draft_validate",
        response_model=DraftValidationResponseDto,
        responses=_WRITE_RESPONSES,
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def draft_validate(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        body: ValidateDraftRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        actor_id = _account_id(principal)
        body_data: dict[str, object] = {
            **body.model_dump(mode="json", by_alias=True),
            "expectedRevision": preflight.expected_revision,
        }

        def command(db: Any) -> IdempotentResponse:
            try:
                result = validate_draft(
                    db,
                    namespace=namespace,
                    draft_id=draft_id,
                    actor_id=actor_id,
                    expected_revision=preflight.expected_revision,
                    dependencies=runtime.dependencies,
                )
            except ConfigurationError as error:
                return _problem(error)
            dto = DraftValidationResponseDto.from_domain(result)
            return IdempotentResponse(
                status_code=200,
                body=dto.model_dump(mode="json", by_alias=True),
                headers={"ETag": entity_tag(result.revision)},
            )

        return _execute(
            runtime,
            actor_id=actor_id,
            operation="draft_validate",
            method="POST",
            path=request.url.path,
            key=preflight.idempotency_key,
            body=body_data,
            command=command,
        )

    @router.get(
        "/policies/{namespace}/drafts/{draft_id}/preview",
        operation_id="draft_preview",
        response_model=PreviewResponseDto,
        responses=_WRITE_RESPONSES,
        dependencies=[Depends(_assert_revision_preflight)],
    )
    def draft_preview(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_RevisionPreflight, Depends(_revision_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        actor_id = _account_id(principal)
        try:
            with runtime.engine.begin() as db:
                result = preview(
                    db,
                    namespace=namespace,
                    draft_id=draft_id,
                    actor_id=actor_id,
                    expected_revision=preflight.expected_revision,
                    dependencies=runtime.dependencies,
                )
        except ConfigurationError as error:
            return _render(_problem(error))
        except PolicySnapshotUnavailable:
            return problem_response(503, "Effective policy unavailable")
        dto = PreviewResponseDto.from_domain(result)
        return JSONResponse(
            status_code=200,
            content=dto.model_dump(mode="json", by_alias=True),
            headers={"ETag": entity_tag(result.revision)},
        )

    @router.post(
        "/policies/{namespace}/drafts/{draft_id}/publish",
        operation_id="draft_publish",
        status_code=201,
        response_model=PublishedVersionDto,
        responses=_PUBLISH_RESPONSES,
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def draft_publish(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        body: PublishDraftRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        actor_id = _account_id(principal)
        return _execute_policy_command(
            lambda: _policy_commands(runtime).publish(
                actor_id=actor_id,
                namespace=namespace,
                draft_id=draft_id,
                expected_revision=preflight.expected_revision,
                reason=body.reason,
                totp_code=body.totp_code,
                idempotency_key=preflight.idempotency_key,
            )
        )

    @router.post(
        "/policies/{namespace}/rollback",
        operation_id="policy_rollback",
        status_code=201,
        response_model=DraftResponseDto,
        responses=_PUBLISH_RESPONSES,
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def policy_rollback(
        namespace: Annotated[str, Path(min_length=1)],
        body: RollbackPolicyRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        actor_id = _account_id(principal)
        return _execute_policy_command(
            lambda: _policy_commands(runtime).rollback(
                actor_id=actor_id,
                namespace=namespace,
                scope=body.scope,
                to_version=body.to_version,
                expected_version=preflight.expected_revision,
                reason=body.reason,
                totp_code=body.totp_code,
                idempotency_key=preflight.idempotency_key,
            )
        )

    @router.get(
        "/policies/{namespace}/versions",
        operation_id="policy_versions",
        response_model=PolicyVersionsResponseDto,
        responses=_PROBLEMS,
    )
    def policy_versions_endpoint(
        namespace: Annotated[str, Path(min_length=1)],
        principal: Annotated[Any, Depends(principal_provider)],
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> PolicyVersionsResponseDto | Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        if cursor is not None and (not cursor.isascii() or not cursor.isdecimal()):
            return problem_response(422, "Invalid policy version cursor")
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                items, next_cursor = list_policy_versions(
                    db,
                    namespace,
                    before_version=None if cursor is None else int(cursor),
                    limit=limit,
                )
        except PolicySnapshotUnavailable:
            return problem_response(503, "Effective policy unavailable")
        return PolicyVersionsResponseDto(
            items=[PublishedVersionDto.from_domain(item) for item in items],
            next_cursor=None if next_cursor is None else str(next_cursor),
        )

    return router
