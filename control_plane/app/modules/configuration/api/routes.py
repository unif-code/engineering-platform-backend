from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from control_plane.app.modules.authorization import PLATFORM_CONFIGURATION_MANAGE
from control_plane.app.modules.configuration import (
    ConfigurationDependencies,
    ConfigurationError,
    DraftArchived,
    DraftAuthorizationDenied,
    DraftNotFound,
    DraftOwnerRequired,
    InvalidPolicyValue,
    PolicyLifecycle,
    PolicyRuntimeRegistry,
    PolicySnapshotUnavailable,
    PolicyVerificationFailed,
    PolicyVersionNotFound,
    SourceStale,
    StaleDraftBase,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.api.dto import (
    ApplyDraftRebaseRequestDto,
    CloneDraftRequestDto,
    DraftArchiveTimingResponseDto,
    DraftBaseComparisonResponseDto,
    DraftCloneResponseDto,
    DraftGovernanceRecordsResponseDto,
    DraftListResponseDto,
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
    TakeoverDraftRequestDto,
    ValidateDraftRequestDto,
)
from control_plane.app.modules.configuration.domain.draft_directory import (
    DraftDirectoryOwner,
    DraftDirectoryView,
)
from control_plane.app.modules.configuration.ports.draft_authorization import (
    DraftAuthorizationPort,
)
from control_plane.app.modules.identity import (
    OwnedPolicySnapshotUnavailable,
    PolicyReauthenticationUnavailable,
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
    owners: PolicyRuntimeRegistry
    dependencies: ConfigurationDependencies
    secret_manager: SecretManagerPort
    draft_authorization: DraftAuthorizationPort | None = None


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
    status: int
    if isinstance(error, DraftAuthorizationDenied):
        status, title = (
            error.status_code,
            "Unauthorized" if error.status_code == 401 else "Forbidden",
        )
    elif isinstance(error, DraftNotFound):
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
    namespace: str,
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
        with runtime.owners.resolve(namespace).transaction() as lifecycle:
            execution = execute_idempotent(
                lifecycle.owner,
                actor=actor_id,
                operation=operation,
                key=key,
                fingerprint=fingerprint,
                command=lambda: command(lifecycle),
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


def _execute_policy_command(command: Callable[[], IdempotentResponse]) -> Response:
    try:
        return _render(command())
    except IdempotencyConflict:
        return problem_response(409, "Idempotency conflict")
    except IdempotencyReplayUnavailable:
        return problem_response(409, "Idempotency replay unavailable")
    except (
        OwnedPolicySnapshotUnavailable,
        PolicySnapshotUnavailable,
        PolicyReauthenticationUnavailable,
    ):
        return problem_response(503, "Effective policy unavailable")


def create_configuration_router(
    runtime_provider: Callable[[], ConfigurationHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["configuration"])

    @router.get(
        "/policies/{namespace}/active",
        operation_id="policy_active",
        response_model=PolicySnapshotDto,
        responses=_PROBLEMS,
    )
    def policy_active(
        namespace: str, principal: Annotated[Any, Depends(principal_provider)]
    ) -> PolicySnapshotDto | Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        try:
            with runtime_provider().owners.resolve(namespace).transaction() as lifecycle:
                return PolicySnapshotDto.from_domain(lifecycle.owner.active_snapshot(namespace))
        except PolicySnapshotUnavailable:
            return problem_response(503, "Effective policy unavailable")

    @router.get(
        "/policies/{namespace}/versions/{version}",
        operation_id="policy_version",
        response_model=PolicySnapshotDto,
        responses=_PROBLEMS,
    )
    def policy_version(
        namespace: str,
        version: Annotated[int, Path(ge=1)],
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> PolicySnapshotDto | Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        try:
            with runtime_provider().owners.resolve(namespace).transaction() as lifecycle:
                snapshot = lifecycle.owner.version_snapshot(namespace, "PLATFORM", version)
                if snapshot is None:
                    return problem_response(404, "Policy version not found")
                return PolicySnapshotDto.from_domain(snapshot)
        except PolicySnapshotUnavailable:
            return problem_response(503, "Effective policy unavailable")

    @router.get(
        "/policies/{namespace}/drafts/{draft_id}",
        operation_id="draft_read",
        response_model=DraftResponseDto,
        responses={
            **_WRITE_RESPONSES,
            200: {
                **_WRITE_RESPONSES[200],
                "headers": {
                    **_ETAG_HEADER,
                    "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
                },
            },
        },
    )
    def draft_read(
        namespace: str, draft_id: str, principal: Annotated[Any, Depends(principal_provider)]
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        try:
            with runtime_provider().owners.resolve(namespace).transaction() as lifecycle:
                lifecycle.owner.active_snapshot(namespace)
                draft = lifecycle.owner.draft(draft_id)
                if draft is None or draft.namespace != namespace:
                    return problem_response(404, "Draft not found")
                return JSONResponse(
                    DraftResponseDto.from_domain(draft).model_dump(mode="json", by_alias=True),
                    headers={"ETag": entity_tag(draft.revision), "Cache-Control": "no-store"},
                )
        except PolicySnapshotUnavailable:
            return problem_response(503, "Effective policy unavailable")

    @router.get(
        "/policies/{namespace}/drafts/{draft_id}/base-comparison",
        operation_id="draft_base_comparison",
        response_model=DraftBaseComparisonResponseDto,
        responses={
            **_PROBLEMS,
            200: {
                "description": "Observed Base, Current and saved Draft comparison",
                "headers": {
                    **_ETAG_HEADER,
                    "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
                },
            },
        },
        dependencies=[Depends(_assert_revision_preflight)],
    )
    def draft_base_comparison(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_RevisionPreflight, Depends(_revision_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        try:
            with runtime_provider().owners.resolve(namespace).transaction() as lifecycle:
                result = lifecycle.base_comparison(
                    namespace=namespace,
                    draft_id=draft_id,
                    expected_revision=preflight.expected_revision,
                )
                dto = DraftBaseComparisonResponseDto.from_domain(result)
                return JSONResponse(
                    dto.model_dump(mode="json", by_alias=True),
                    headers={
                        "ETag": entity_tag(result.draft_revision),
                        "Cache-Control": "no-store",
                    },
                )
        except (DraftNotFound, StaleDraftRevision, InvalidPolicyValue) as error:
            return _render(_problem(error))
        except Exception:
            return problem_response(503, "Draft base comparison unavailable")

    @router.get(
        "/policies/{namespace}/drafts/{draft_id}/archive-timing",
        operation_id="draft_archive_timing",
        response_model=DraftArchiveTimingResponseDto,
        responses={
            **_PROBLEMS,
            200: {
                "description": "Inactivity timing observed against the current owner policy",
                "headers": {
                    **_ETAG_HEADER,
                    "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
                },
            },
        },
        dependencies=[Depends(_assert_revision_preflight)],
    )
    def draft_archive_timing(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_RevisionPreflight, Depends(_revision_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        try:
            with runtime_provider().owners.resolve(namespace).transaction() as lifecycle:
                result = lifecycle.archive_timing(
                    namespace=namespace,
                    draft_id=draft_id,
                    expected_revision=preflight.expected_revision,
                )
                dto = DraftArchiveTimingResponseDto.from_domain(result)
                return JSONResponse(
                    dto.model_dump(mode="json", by_alias=True),
                    headers={
                        "ETag": entity_tag(result.draft.revision),
                        "Cache-Control": "no-store",
                    },
                )
        except DraftNotFound:
            return problem_response(404, "Draft not found")
        except StaleDraftRevision:
            return problem_response(409, "Draft archive timing changed")
        except Exception:
            return problem_response(503, "Draft archive timing unavailable")

    @router.get(
        "/policies/{namespace}/drafts/{draft_id}/governance-records",
        operation_id="draft_governance_records",
        response_model=DraftGovernanceRecordsResponseDto,
        responses={
            **_PROBLEMS,
            200: {
                "description": "Recorded Clone and Rebase facts at the observed draft revision",
                "headers": {
                    **_ETAG_HEADER,
                    "Cache-Control": {
                        "schema": {"type": "string", "const": "no-store"},
                    },
                },
            },
        },
        dependencies=[Depends(_assert_revision_preflight)],
    )
    def draft_governance_records(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_RevisionPreflight, Depends(_revision_preflight)],
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        try:
            with runtime_provider().owners.resolve(namespace).transaction() as lifecycle:
                result = lifecycle.governance_records(
                    namespace=namespace,
                    draft_id=draft_id,
                    expected_revision=preflight.expected_revision,
                    cursor=cursor,
                    limit=limit,
                )
                dto = DraftGovernanceRecordsResponseDto.from_domain(result)
                return JSONResponse(
                    dto.model_dump(mode="json", by_alias=True),
                    headers={
                        "ETag": entity_tag(result.draft_revision),
                        "Cache-Control": "no-store",
                    },
                )
        except (DraftNotFound, StaleDraftRevision, InvalidPolicyValue) as error:
            return _render(_problem(error))
        except Exception:
            return problem_response(503, "Draft governance records unavailable")

    @router.get(
        "/policies",
        operation_id="policies_catalog",
        response_model=PolicyCatalogResponseDto,
        responses=_PROBLEMS,
    )
    def policies_catalog(
        principal: Annotated[Any, Depends(principal_provider)],
        namespace: Annotated[str, Query()] = "identity",
    ) -> PolicyCatalogResponseDto | Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        try:
            with runtime.owners.resolve(namespace).transaction() as lifecycle:
                keys = lifecycle.owner.catalog(namespace)
                snapshot = lifecycle.owner.active_snapshot(namespace)
        except PolicySnapshotUnavailable:
            return problem_response(503, "Effective policy unavailable")
        return PolicyCatalogResponseDto(
            items=[PolicyKeyDto.from_domain(key) for key in keys],
            active=PolicySnapshotDto.from_domain(snapshot),
        )

    @router.get(
        "/policies/{namespace}/drafts",
        operation_id="draft_list",
        response_model=DraftListResponseDto,
        responses={
            **_PROBLEMS,
            200: {
                "description": "Draft metadata observed against the current policy version",
                "headers": {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}},
            },
        },
    )
    def draft_list(
        namespace: Annotated[str, Path(min_length=1)],
        principal: Annotated[Any, Depends(principal_provider)],
        view: Annotated[DraftDirectoryView, Query()] = "ALL",
        owner: Annotated[DraftDirectoryOwner, Query()] = "ALL",
        cursor: Annotated[str | None, Query()] = None,
        current_version: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        actor_id = _account_id(principal)
        try:
            with runtime_provider().owners.resolve(namespace).transaction() as lifecycle:
                result = lifecycle.draft_directory(
                    namespace=namespace,
                    actor_id=actor_id,
                    view=view,
                    owner=owner,
                    cursor=cursor,
                    current_version=current_version,
                    limit=limit,
                )
                dto = DraftListResponseDto.from_domain(result)
                return JSONResponse(
                    dto.model_dump(mode="json", by_alias=True),
                    headers={"Cache-Control": "no-store"},
                )
        except InvalidPolicyValue:
            return problem_response(422, "Invalid draft directory query")
        except StaleDraftBase:
            return problem_response(409, "Draft directory Current changed")
        except Exception:
            return problem_response(503, "Draft directory unavailable")

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

        def command(lifecycle: PolicyLifecycle) -> IdempotentResponse:
            try:
                draft = lifecycle.create_draft(
                    namespace=namespace,
                    values=body.values,
                    actor_id=actor_id,
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
            namespace=namespace,
            operation="draft_create",
            method="POST",
            path=request.url.path,
            key=preflight.idempotency_key,
            body=body_data,
            command=command,
        )

    @router.post(
        "/policies/{namespace}/drafts/{draft_id}/takeover",
        operation_id="draft_takeover",
        response_model=DraftResponseDto,
        responses=_WRITE_RESPONSES,
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def draft_takeover(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        body: TakeoverDraftRequestDto,
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

        def command(lifecycle: PolicyLifecycle) -> IdempotentResponse:
            try:
                draft = lifecycle.takeover_draft(
                    namespace=namespace,
                    draft_id=draft_id,
                    actor_id=actor_id,
                    expected_revision=preflight.expected_revision,
                    reason=body.reason,
                )
            except ConfigurationError as error:
                return _problem(error)
            return IdempotentResponse(
                status_code=200,
                body=DraftResponseDto.from_domain(draft).model_dump(mode="json", by_alias=True),
                headers={"ETag": entity_tag(draft.revision)},
            )

        return _execute(
            runtime,
            actor_id=actor_id,
            namespace=namespace,
            operation="draft_takeover",
            method="POST",
            path=request.url.path,
            key=preflight.idempotency_key,
            body=body_data,
            command=command,
        )

    @router.post(
        "/policies/{namespace}/drafts/{draft_id}/rebase",
        operation_id="draft_rebase_apply",
        response_model=DraftResponseDto,
        responses=_WRITE_RESPONSES,
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def draft_rebase_apply(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        body: ApplyDraftRebaseRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        actor_id = _account_id(principal)
        body_data = body.model_dump(mode="json", by_alias=True)

        def command(lifecycle: PolicyLifecycle) -> IdempotentResponse:
            try:
                result = lifecycle.apply_rebase(
                    namespace=namespace,
                    draft_id=draft_id,
                    actor_id=actor_id,
                    expected_revision=preflight.expected_revision,
                    request=body_data,
                    raw_session=request.cookies.get("ep_session", ""),
                    authorization=runtime.draft_authorization,
                )
            except ConfigurationError as error:
                return _problem(error)
            return IdempotentResponse(
                status_code=200,
                body=DraftResponseDto.from_domain(result).model_dump(mode="json", by_alias=True),
                headers={"ETag": entity_tag(result.revision)},
            )

        return _execute(
            runtime,
            actor_id=actor_id,
            namespace=namespace,
            operation="draft_rebase_apply",
            method="POST",
            path=request.url.path,
            key=preflight.idempotency_key,
            body={**body_data, "expectedRevision": preflight.expected_revision},
            command=command,
        )

    @router.post(
        "/policies/{namespace}/drafts/{draft_id}/clone",
        operation_id="draft_clone",
        status_code=201,
        response_model=DraftCloneResponseDto,
        responses=_CREATE_RESPONSES,
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def draft_clone(
        namespace: Annotated[str, Path(min_length=1)],
        draft_id: Annotated[str, Path(min_length=1)],
        body: CloneDraftRequestDto,
        request: Request,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        capability_guard(principal, PLATFORM_CONFIGURATION_MANAGE, None)
        runtime = runtime_provider()
        actor_id = _account_id(principal)

        def command(lifecycle: PolicyLifecycle) -> IdempotentResponse:
            try:
                result = lifecycle.clone_draft(
                    namespace=namespace,
                    draft_id=draft_id,
                    actor_id=actor_id,
                    expected_revision=preflight.expected_revision,
                    raw_session=request.cookies.get("ep_session", ""),
                    authorization=runtime.draft_authorization,
                )
            except ConfigurationError as error:
                return _problem(error)
            return IdempotentResponse(
                status_code=201,
                body=DraftCloneResponseDto.from_domain(result).model_dump(
                    mode="json", by_alias=True
                ),
                headers={"ETag": entity_tag(result.draft.revision)},
            )

        return _execute(
            runtime,
            actor_id=actor_id,
            namespace=namespace,
            operation="draft_clone",
            method="POST",
            path=request.url.path,
            key=preflight.idempotency_key,
            body={
                **body.model_dump(mode="json", by_alias=True),
                "expectedRevision": preflight.expected_revision,
            },
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

        def command(lifecycle: PolicyLifecycle) -> IdempotentResponse:
            try:
                draft = lifecycle.update_draft(
                    namespace=namespace,
                    draft_id=draft_id,
                    values=body.values,
                    actor_id=actor_id,
                    expected_revision=preflight.expected_revision,
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
            namespace=namespace,
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

        def command(lifecycle: PolicyLifecycle) -> IdempotentResponse:
            try:
                result = lifecycle.validate_draft(
                    namespace=namespace,
                    draft_id=draft_id,
                    actor_id=actor_id,
                    expected_revision=preflight.expected_revision,
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
            namespace=namespace,
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
            with runtime.owners.resolve(namespace).transaction() as lifecycle:
                result = lifecycle.preview(
                    namespace=namespace,
                    draft_id=draft_id,
                    actor_id=actor_id,
                    expected_revision=preflight.expected_revision,
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
            lambda: runtime.owners.resolve(namespace).publish(
                raw_session=request.cookies.get("ep_session", ""),
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
            lambda: runtime.owners.resolve(namespace).rollback(
                raw_session=request.cookies.get("ep_session", ""),
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
            with runtime.owners.resolve(namespace).transaction() as lifecycle:
                items = lifecycle.owner.list_versions(
                    namespace,
                    "PLATFORM",
                    before_version=None if cursor is None else int(cursor),
                    limit=limit + 1,
                )
                next_cursor = items[limit - 1].version if len(items) > limit else None
                items = items[:limit]
        except PolicySnapshotUnavailable:
            return problem_response(503, "Effective policy unavailable")
        return PolicyVersionsResponseDto(
            items=[PublishedVersionDto.from_domain(item) for item in items],
            next_cursor=None if next_cursor is None else str(next_cursor),
        )

    return router
