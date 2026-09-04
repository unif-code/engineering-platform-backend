import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr, ValidationError

from control_plane.app.modules.agent_run.api.dto import (
    CancelExecutionRequestDto,
    EvidenceCleanupRequestDto,
    HandoffToChildRequestDto,
    LifecycleReceiptDto,
    MaterializationReadyDto,
    MaterializationStatusDto,
    PreviewPublishedDto,
    ProvisionMaterializationRequestDto,
    PublishPreviewRequestDto,
    ReconcileLeaseRequestDto,
    ReconciliationReceiptDto,
    SandboxApiModel,
)
from control_plane.app.modules.agent_run.api.runtime import (
    SandboxHttpRuntime,
    ServiceIdentityUnavailable,
    WorkloadPrincipal,
)
from control_plane.app.modules.agent_run.application.errors import SandboxApplicationError
from control_plane.app.modules.agent_run.domain import (
    CancelExecutionCommand,
    CheckpointAndReleaseCommand,
    CommandContext,
    DenialCode,
    ExecutionKind,
    ExecutionRef,
    FinalizeExecutionCommand,
    GetMaterializationStatusQuery,
    HandoffToChildCommand,
    LifecycleReceipt,
    MaterializationBlocked,
    MaterializationFailed,
    MaterializationReady,
    PreviewPublished,
    ProvisionMaterializationCommand,
    PublishPreviewCommand,
    ReconcileLeaseCommand,
    SandboxDenied,
)
from control_plane.app.modules.agent_run.domain.policy import SandboxPolicyViolation
from control_plane.app.modules.agent_run.ports import SandboxPort
from control_plane.app.shared.api.concurrency import entity_tag, require_if_match
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import problem_response
from control_plane.app.shared.api.request_id import current_request_id
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
)

RuntimeProvider = Callable[[], SandboxHttpRuntime]
_PREFIX = "/api/v1/internal/sandbox"
_BEARER = HTTPBearer(auto_error=False, scheme_name="WorkloadBearer")
_ETAG_HEADER = {
    "ETag": {
        "description": "Strong materialization or reconciliation entity tag",
        "schema": {"type": "string"},
    }
}


class SandboxProblemDto(SandboxApiModel):
    title: str
    status: int
    detail: str | None = None
    request_id: str | None = None
    code: str | None = None
    failure_dimension: str | None = None
    retryable: bool | None = None
    revision: int | None = None


_PROBLEM_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        status: {"description": title, "model": SandboxProblemDto}
        for status, title in (
            (401, "Unauthorized"),
            (403, "Forbidden"),
            (404, "Not found"),
            (409, "Conflict"),
            (422, "Validation failed"),
            (500, "Internal server error"),
            (503, "Service unavailable"),
        )
    },
)
_OK_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {**_PROBLEM_RESPONSES, 200: {"description": "Success", "headers": _ETAG_HEADER}},
)
_CREATED_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {**_PROBLEM_RESPONSES, 201: {"description": "Created", "headers": _ETAG_HEADER}},
)


@dataclass(frozen=True, slots=True)
class _MutationPreflight:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _VersionedMutationPreflight:
    idempotency_key: str
    expected_revision: int


def _mutation_preflight(
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> _MutationPreflight:
    return _MutationPreflight(idempotency_key=idempotency_key)


def _versioned_mutation_preflight(
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    expected_revision: Annotated[int, Depends(require_if_match)],
) -> _VersionedMutationPreflight:
    return _VersionedMutationPreflight(
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
    )


def _controller(runtime: SandboxHttpRuntime) -> SandboxPort:
    if runtime.controller is None:
        raise HTTPException(status_code=503, detail="Sandbox Controller unavailable")
    return runtime.controller


def _context(
    request: Request,
    principal: WorkloadPrincipal,
    idempotency_key: str,
) -> CommandContext:
    request_id = current_request_id()
    return CommandContext(
        idempotency_key=idempotency_key,
        actor=principal.actor,
        correlation_id=request_id or f"sandbox:{idempotency_key}",
        request_id=request_id,
    )


def _etag(body: dict[str, Any], *, revision: int | None = None) -> str:
    if revision is not None:
        return entity_tag(revision)
    canonical = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f'"sha256-{hashlib.sha256(canonical.encode("utf-8")).hexdigest()}"'


def _success(
    dto: SandboxApiModel,
    *,
    status_code: int,
    revision: int | None = None,
) -> JSONResponse:
    body = dict(dto.model_dump(mode="json", by_alias=True))
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={"ETag": _etag(body, revision=revision)},
    )


def _denial_status(
    code: DenialCode,
    failure_dimension: str | None,
) -> tuple[int, str]:
    if code is DenialCode.RUNTIME_BINDING_INVALID and failure_dimension == "active_execution":
        return 409, "Active sandbox execution conflict"
    return {
        DenialCode.CAPACITY_UNAVAILABLE: (503, "Sandbox capacity unavailable"),
        DenialCode.POLICY_LIMIT_REACHED: (409, "Sandbox policy limit reached"),
        DenialCode.POLICY_DISABLED: (403, "Sandbox policy disabled"),
        DenialCode.RUNTIME_BINDING_INVALID: (422, "Sandbox binding invalid"),
        DenialCode.RUNTIME_CAPABILITY_DENIED: (403, "Sandbox capability denied"),
        DenialCode.RUNTIME_BOUNDARY_VIOLATION: (403, "Sandbox boundary violation"),
        DenialCode.STALE_RUNNER_GENERATION: (409, "Stale runner generation"),
        DenialCode.RESOURCE_EXHAUSTED: (503, "Sandbox resource exhausted"),
    }[code]


def _denial_response(denial: Any, *, revision: int | None = None) -> Response:
    status, title = _denial_status(denial.code, denial.failure_dimension)
    extra: dict[str, object] = {
        "code": denial.code.value,
        "retryable": denial.retryable,
    }
    if denial.failure_dimension is not None:
        extra["failureDimension"] = denial.failure_dimension
    if revision is not None:
        extra["revision"] = revision
    headers = {"ETag": entity_tag(revision)} if revision is not None else None
    return problem_response(status, title, extra=extra, headers=headers)


def _lifecycle_response(value: LifecycleReceipt) -> Response:
    if value.denial is not None:
        return _denial_response(value.denial, revision=value.revision)
    return _success(
        LifecycleReceiptDto.from_domain(value),
        status_code=200,
        revision=value.revision,
    )


def create_sandbox_router(runtime_provider: RuntimeProvider) -> APIRouter:
    router = APIRouter(prefix=_PREFIX, tags=["sandbox-controller"])

    def runtime() -> SandboxHttpRuntime:
        try:
            return runtime_provider()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Sandbox Controller unavailable",
            ) from exc

    def principal(
        sandbox_runtime: Annotated[SandboxHttpRuntime, Depends(runtime)],
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ] = None,
    ) -> WorkloadPrincipal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            resolved = sandbox_runtime.identity_verifier.verify(SecretStr(credentials.credentials))
        except ServiceIdentityUnavailable:
            raise HTTPException(
                status_code=503,
                detail="Workload identity unavailable",
            ) from None
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="Workload identity unavailable",
            ) from None
        if resolved is None:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return resolved

    @router.post(
        "/materializations",
        operation_id="sandbox_provision_materialization",
        status_code=201,
        response_model=MaterializationReadyDto,
        responses=_CREATED_RESPONSES,
    )
    def provision_materialization(
        body: ProvisionMaterializationRequestDto,
        request: Request,
        preflight: Annotated[_MutationPreflight, Depends(_mutation_preflight)],
        actor: Annotated[WorkloadPrincipal, Depends(principal)],
        sandbox_runtime: Annotated[SandboxHttpRuntime, Depends(runtime)],
    ) -> Response:
        try:
            binding = body.to_domain()
        except ValidationError:
            raise HTTPException(status_code=422, detail="Validation failed") from None
        result = _controller(sandbox_runtime).provision_materialization(
            ProvisionMaterializationCommand(
                context=_context(request, actor, preflight.idempotency_key),
                binding=binding,
            )
        )
        if isinstance(result, MaterializationReady):
            return _success(
                MaterializationReadyDto.from_domain(result),
                status_code=201,
                revision=result.handle.revision,
            )
        if isinstance(result, (MaterializationBlocked, MaterializationFailed)):
            return _denial_response(result.denial)
        raise RuntimeError("unreachable provision result")

    @router.get(
        "/materializations/{materialization_id}",
        operation_id="sandbox_get_materialization_status",
        response_model=MaterializationStatusDto,
        responses=_OK_RESPONSES,
    )
    def get_materialization_status(
        materialization_id: Annotated[str, Path(min_length=1)],
        actor: Annotated[WorkloadPrincipal, Depends(principal)],
        sandbox_runtime: Annotated[SandboxHttpRuntime, Depends(runtime)],
    ) -> Response:
        result = _controller(sandbox_runtime).get_materialization_status(
            GetMaterializationStatusQuery(
                actor=actor.actor,
                materialization_id=materialization_id,
            )
        )
        return _success(
            MaterializationStatusDto.from_domain(result),
            status_code=200,
            revision=result.revision,
        )

    @router.post(
        "/materializations/{materialization_id}/preview",
        operation_id="sandbox_publish_preview",
        status_code=201,
        response_model=PreviewPublishedDto,
        responses=_CREATED_RESPONSES,
    )
    def publish_preview(
        materialization_id: Annotated[str, Path(min_length=1)],
        body: PublishPreviewRequestDto,
        request: Request,
        preflight: Annotated[
            _VersionedMutationPreflight,
            Depends(_versioned_mutation_preflight),
        ],
        actor: Annotated[WorkloadPrincipal, Depends(principal)],
        sandbox_runtime: Annotated[SandboxHttpRuntime, Depends(runtime)],
    ) -> Response:
        result = _controller(sandbox_runtime).publish_preview(
            PublishPreviewCommand(
                context=_context(request, actor, preflight.idempotency_key),
                guard=body.to_guard(materialization_id, preflight.expected_revision),
                metadata=body.metadata.to_domain(),
                expires_at=body.expires_at,
            )
        )
        if isinstance(result, SandboxDenied):
            return _denial_response(result.denial, revision=result.revision)
        if isinstance(result, PreviewPublished):
            return _success(
                PreviewPublishedDto.from_domain(result),
                status_code=201,
                revision=result.revision,
            )
        raise RuntimeError("unreachable preview result")

    @router.post(
        "/materializations/{materialization_id}/checkpoint-release",
        operation_id="sandbox_checkpoint_and_release",
        response_model=LifecycleReceiptDto,
        responses=_OK_RESPONSES,
    )
    def checkpoint_and_release(
        materialization_id: Annotated[str, Path(min_length=1)],
        body: EvidenceCleanupRequestDto,
        request: Request,
        preflight: Annotated[
            _VersionedMutationPreflight,
            Depends(_versioned_mutation_preflight),
        ],
        actor: Annotated[WorkloadPrincipal, Depends(principal)],
        sandbox_runtime: Annotated[SandboxHttpRuntime, Depends(runtime)],
    ) -> Response:
        result = _controller(sandbox_runtime).checkpoint_and_release(
            CheckpointAndReleaseCommand(
                context=_context(request, actor, preflight.idempotency_key),
                guard=body.to_guard(materialization_id, preflight.expected_revision),
                evidence_refs=body.domain_evidence_refs(),
            )
        )
        return _lifecycle_response(result)

    @router.post(
        "/materializations/{materialization_id}/handoff",
        operation_id="sandbox_handoff_to_child",
        response_model=LifecycleReceiptDto,
        responses=_OK_RESPONSES,
    )
    def handoff_to_child(
        materialization_id: Annotated[str, Path(min_length=1)],
        body: HandoffToChildRequestDto,
        request: Request,
        preflight: Annotated[
            _VersionedMutationPreflight,
            Depends(_versioned_mutation_preflight),
        ],
        actor: Annotated[WorkloadPrincipal, Depends(principal)],
        sandbox_runtime: Annotated[SandboxHttpRuntime, Depends(runtime)],
    ) -> Response:
        result = _controller(sandbox_runtime).handoff_to_child(
            HandoffToChildCommand(
                context=_context(request, actor, preflight.idempotency_key),
                guard=body.to_guard(materialization_id, preflight.expected_revision),
                child_execution_id=body.child_execution_id,
            )
        )
        return _lifecycle_response(result)

    @router.post(
        "/materializations/{materialization_id}/finalize",
        operation_id="sandbox_finalize_execution",
        response_model=LifecycleReceiptDto,
        responses=_OK_RESPONSES,
    )
    def finalize_execution(
        materialization_id: Annotated[str, Path(min_length=1)],
        body: EvidenceCleanupRequestDto,
        request: Request,
        preflight: Annotated[
            _VersionedMutationPreflight,
            Depends(_versioned_mutation_preflight),
        ],
        actor: Annotated[WorkloadPrincipal, Depends(principal)],
        sandbox_runtime: Annotated[SandboxHttpRuntime, Depends(runtime)],
    ) -> Response:
        result = _controller(sandbox_runtime).finalize_execution(
            FinalizeExecutionCommand(
                context=_context(request, actor, preflight.idempotency_key),
                guard=body.to_guard(materialization_id, preflight.expected_revision),
                evidence_refs=body.domain_evidence_refs(),
            )
        )
        return _lifecycle_response(result)

    @router.post(
        "/executions/{execution_id}/cancel",
        operation_id="sandbox_cancel_execution",
        response_model=LifecycleReceiptDto,
        responses=_OK_RESPONSES,
    )
    def cancel_execution(
        execution_id: Annotated[str, Path(min_length=1)],
        body: CancelExecutionRequestDto,
        request: Request,
        preflight: Annotated[_MutationPreflight, Depends(_mutation_preflight)],
        actor: Annotated[WorkloadPrincipal, Depends(principal)],
        sandbox_runtime: Annotated[SandboxHttpRuntime, Depends(runtime)],
    ) -> Response:
        result = _controller(sandbox_runtime).cancel_execution(
            CancelExecutionCommand(
                context=_context(request, actor, preflight.idempotency_key),
                execution=ExecutionRef(
                    execution_id=execution_id,
                    kind=ExecutionKind.SINGLE_REPOSITORY_FIX,
                ),
                reason=body.reason,
            )
        )
        return _lifecycle_response(result)

    @router.post(
        "/leases/reconcile",
        operation_id="sandbox_reconcile_lease",
        response_model=ReconciliationReceiptDto,
        responses=_OK_RESPONSES,
    )
    def reconcile_lease(
        body: ReconcileLeaseRequestDto,
        request: Request,
        preflight: Annotated[_MutationPreflight, Depends(_mutation_preflight)],
        actor: Annotated[WorkloadPrincipal, Depends(principal)],
        sandbox_runtime: Annotated[SandboxHttpRuntime, Depends(runtime)],
    ) -> Response:
        result = _controller(sandbox_runtime).reconcile_lease(
            ReconcileLeaseCommand(
                context=_context(request, actor, preflight.idempotency_key),
                environment_id=body.environment_id,
                execution_id=body.execution_id,
                observed_at=body.observed_at,
            )
        )
        return _success(
            ReconciliationReceiptDto.from_domain(result),
            status_code=200,
        )

    return router


def register_sandbox_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(SandboxApplicationError)
    async def sandbox_application_error(_: Request, error: SandboxApplicationError) -> Response:
        return _denial_response(error)

    @app.exception_handler(SandboxPolicyViolation)
    async def sandbox_policy_error(_: Request, error: SandboxPolicyViolation) -> Response:
        return _denial_response(error)

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict(_: Request, __: IdempotencyConflict) -> Response:
        return problem_response(
            409,
            "Idempotency conflict",
            extra={"code": "IDEMPOTENCY_CONFLICT"},
        )

    @app.exception_handler(IdempotencyReplayUnavailable)
    async def replay_unavailable(_: Request, __: IdempotencyReplayUnavailable) -> Response:
        return problem_response(
            503,
            "Idempotency replay unavailable",
            extra={"code": DenialCode.RESOURCE_EXHAUSTED.value},
        )
