from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from control_plane.app.modules.agent import (
    cancel_attempt,
    get_business_context_status,
    get_run,
    get_run_metadata,
    list_definitions,
    list_events,
    list_runs,
    resume_attempt,
    start_run,
)
from control_plane.app.modules.agent.api.dto import (
    AgentDefinitionListResponseDto,
    AgentRunBusinessContextStatusResponseDto,
    AgentRunDetailsResponseDto,
    AgentRunListResponseDto,
    AttemptControlRequestDto,
    AttemptControlResponseDto,
    CanonicalEventPageResponseDto,
    StartAgentRunRequestDto,
    StartAgentRunResponseDto,
    json_content,
)
from control_plane.app.modules.agent.api.runtime import (
    AgentHttpRuntime,
    CurrentPrincipalActorResolver,
    LazyRequirementExecutionContext,
)
from control_plane.app.modules.agent.application.control import (
    AgentAttemptNotFound,
    AttemptRevisionConflict,
    AttemptWaitingExpired,
    BindingDigestMismatch,
    CancelAttemptCommand,
    ResumeAttemptCommand,
)
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.errors import (
    AgentBusinessContextReason,
    InvalidRequirementExecutionContext,
)
from control_plane.app.modules.agent.application.events import EventReplayUnavailable
from control_plane.app.modules.agent.application.idempotency import AgentReplayUnavailable
from control_plane.app.modules.agent.application.queries import (
    AgentBusinessContextCurrentness,
    AgentRunBusinessContextStatus,
    AgentRunNotFound,
    InvalidEventCursor,
    InvalidEventPageLimit,
    InvalidRunCursor,
    InvalidRunPageLimit,
)
from control_plane.app.modules.agent.application.runs import (
    DefinitionUnavailable,
    IdempotencyConflict,
    IdempotencyInProgress,
    StartRunCommand,
)
from control_plane.app.modules.agent.domain import (
    AgentDomainError,
    AgentQueryUnavailable,
    RepositoryWriteForbidden,
    RunState,
)
from control_plane.app.modules.requirement import (
    RequirementDependencyUnavailable,
    RequirementNotFound,
)
from control_plane.app.shared.api.concurrency import entity_tag, require_if_match
from control_plane.app.shared.api.idempotency import require_idempotency_key
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    problem_response,
)
from control_plane.app.shared.api.request_id import current_request_id
from control_plane.app.shared.security import SecretMaterialUnavailable, assert_same_origin

AGENT_DEFINITION_READ_CAPABILITY = "agent.definition.read"
AGENT_RUN_EXECUTE_CAPABILITY = "agent.run.execute"
AGENT_RUN_READ_CAPABILITY = "agent.run.read"
AGENT_RUN_CONTROL_CAPABILITY = "agent.run.control"

_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)
_ATTEMPT_ETAG_HEADER = {
    "ETag": {
        "description": "Strong Agent Attempt revision entity tag",
        "schema": {"type": "string", "pattern": '^"v[1-9][0-9]*"$'},
    }
}


@dataclass(frozen=True, slots=True)
class _CreatePreflight:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _VersionedPreflight:
    idempotency_key: str
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


def _problem(error: Exception) -> Response:
    for error_type, code in (
        (AttemptRevisionConflict, "ATTEMPT_REVISION_CONFLICT"),
        (IdempotencyConflict, "IDEMPOTENCY_CONFLICT"),
        (IdempotencyInProgress, "IDEMPOTENCY_IN_PROGRESS"),
    ):
        if isinstance(error, error_type):
            return problem_response(409, "Agent state conflict", extra={"code": code})
    if isinstance(error, AgentReplayUnavailable):
        return problem_response(
            503, "Agent service unavailable", extra={"code": "AGENT_REPLAY_UNAVAILABLE"}
        )
    if isinstance(error, (AgentRunNotFound, AgentAttemptNotFound, RequirementNotFound)):
        return problem_response(404, "Agent subject not found")
    if isinstance(error, InvalidEventCursor):
        return problem_response(422, "Invalid Agent event cursor")
    if isinstance(error, InvalidRunCursor):
        return problem_response(422, "Invalid Agent Run cursor")
    if isinstance(
        error, (InvalidEventPageLimit, InvalidRunPageLimit, InvalidRequirementExecutionContext)
    ):
        return problem_response(422, "Invalid Agent input")
    if isinstance(
        error,
        (
            AttemptWaitingExpired,
            BindingDigestMismatch,
            DefinitionUnavailable,
            AgentDomainError,
            RepositoryWriteForbidden,
        ),
    ):
        return problem_response(409, "Agent state conflict")
    if isinstance(error, ValidationError):
        return problem_response(422, "Invalid Agent input")
    if isinstance(
        error,
        (
            RequirementDependencyUnavailable,
            SQLAlchemyError,
            SecretMaterialUnavailable,
            EventReplayUnavailable,
            AgentQueryUnavailable,
        ),
    ):
        return problem_response(503, "Agent service unavailable")
    raise error


def _employee_id(principal: Any) -> str:
    employee_id = getattr(principal, "employee_id", None)
    if not isinstance(employee_id, str) or not employee_id:
        raise HTTPException(status_code=503, detail="Authorization unavailable")
    return employee_id


def _request_dependencies(
    runtime: AgentHttpRuntime, principal: Any
) -> tuple[AgentDependencies, str]:
    employee_id = _employee_id(principal)
    return (
        runtime.dependencies.with_actor_resolver(CurrentPrincipalActorResolver(employee_id)),
        employee_id,
    )


def _json(dto: Any, *, status_code: int, revision: int | None = None) -> JSONResponse:
    headers = {} if revision is None else {"ETag": entity_tag(revision)}
    return JSONResponse(
        status_code=status_code,
        content=json_content(dto),
        headers=headers,
    )


def create_agent_router(
    runtime_provider: Callable[[], AgentHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(tags=["agent"])

    @router.get(
        "/api/v1/agent-runs",
        operation_id="agent_runs_list",
        response_model=AgentRunListResponseDto,
        responses=_RESPONSES,
    )
    def agent_runs_list(
        workspace_id: Annotated[UUID, Query(alias="workspaceId")],
        principal: Annotated[Any, Depends(principal_provider)],
        state: Annotated[RunState | None, Query()] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Response:
        capability_guard(principal, AGENT_RUN_READ_CAPABILITY, str(workspace_id))
        try:
            page = list_runs(
                None,
                workspace_id=str(workspace_id),
                state=state,
                cursor=cursor,
                limit=limit,
                dependencies=runtime_provider().dependencies,
            )
            return JSONResponse(
                json_content(AgentRunListResponseDto.from_domain(page)),
                headers={"Cache-Control": "no-store"},
            )
        except Exception as error:
            return _problem(error)

    @router.get(
        "/api/v1/agent-definitions",
        operation_id="agent_definitions_list",
        response_model=AgentDefinitionListResponseDto,
        responses={
            **_RESPONSES,
            200: {
                "headers": {
                    "Cache-Control": {
                        "description": (
                            "Definition declaration observation; no cached execution authority"
                        ),
                        "schema": {"type": "string", "const": "no-store"},
                    }
                }
            },
        },
    )
    def agent_definitions_list(
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> Response | AgentDefinitionListResponseDto:
        capability_guard(principal, AGENT_DEFINITION_READ_CAPABILITY, None)
        try:
            definitions = list_definitions(None, dependencies=runtime_provider().dependencies)
            return JSONResponse(
                json_content(AgentDefinitionListResponseDto.from_domain(definitions)),
                headers={"Cache-Control": "no-store"},
            )
        except Exception as error:
            if isinstance(error, StarletteHTTPException) and error.status_code in (401, 403):
                raise
            return problem_response(503, "Agent service unavailable")

    @router.post(
        "/api/v1/agent-runs",
        operation_id="agent_runs_start",
        status_code=202,
        response_model=StartAgentRunResponseDto,
        responses={**_RESPONSES, 202: {"headers": _ATTEMPT_ETAG_HEADER}},
        dependencies=[Depends(_assert_create_preflight), Depends(_create_preflight)],
    )
    def agent_runs_start(
        body: StartAgentRunRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_CreatePreflight, Depends(_create_preflight)],
    ) -> Response:
        workspace_id = str(body.workspace_id)
        capability_guard(principal, AGENT_RUN_EXECUTE_CAPABILITY, workspace_id)
        try:
            runtime = runtime_provider()
            dependencies, actor = _request_dependencies(runtime, principal)
            with runtime.requirement_engine.connect() as requirement_db:
                dependencies = dependencies.with_requirement_context(
                    runtime.requirement_context_factory(requirement_db)
                )
                result = start_run(
                    None,
                    command=StartRunCommand(
                        workspace_id=workspace_id,
                        requirement_id=str(body.requirement_id),
                        work_item_id=str(body.work_item_id),
                        definition_id=str(body.definition_id),
                        definition_version=body.definition_version,
                        goal=body.goal,
                        actor=actor,
                        idempotency_key=preflight.idempotency_key,
                        correlation_id=current_request_id() or runtime.dependencies.new_id(),
                    ),
                    dependencies=dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            StartAgentRunResponseDto.from_domain(result),
            status_code=202,
            revision=result.attempt.revision,
        )

    @router.get(
        "/api/v1/agent-runs/{runId}/business-context-status",
        operation_id="agent_run_business_context_status_get",
        response_model=AgentRunBusinessContextStatusResponseDto,
        responses={
            **_RESPONSES,
            200: {
                "headers": {
                    "Cache-Control": {
                        "description": "Fresh association observation; no execution authority",
                        "schema": {"type": "string", "const": "no-store"},
                    }
                }
            },
        },
    )
    def agent_run_business_context_status_get(
        run_id: Annotated[UUID, Path(alias="runId")],
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> Response:
        try:
            runtime = runtime_provider()
            run = get_run_metadata(None, run_id=str(run_id), dependencies=runtime.dependencies)
        except Exception as error:
            return _problem(error)
        capability_guard(principal, AGENT_RUN_READ_CAPABILITY, run.workspace_id)
        if run.business_context is None:
            result = get_business_context_status(None, run=run, dependencies=runtime.dependencies)
        else:
            try:
                with runtime.requirement_engine.connect() as requirement_db:
                    dependencies = runtime.dependencies.with_requirement_context(
                        runtime.requirement_context_factory(requirement_db)
                    )
                    result = get_business_context_status(None, run=run, dependencies=dependencies)
            except Exception as error:
                if isinstance(error, StarletteHTTPException) and error.status_code in (401, 403):
                    raise
                result = AgentRunBusinessContextStatus(
                    run_id=run.id,
                    workspace_id=run.workspace_id,
                    checked_at=runtime.dependencies.clock(),
                    currentness=AgentBusinessContextCurrentness.UNVERIFIABLE,
                    reasons=(AgentBusinessContextReason.OWNER_UNAVAILABLE,),
                )
        response = _json(
            AgentRunBusinessContextStatusResponseDto.from_domain(result), status_code=200
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/api/v1/agent-runs/{runId}",
        operation_id="agent_runs_get",
        response_model=AgentRunDetailsResponseDto,
        responses={**_RESPONSES, 200: {"headers": _ATTEMPT_ETAG_HEADER}},
    )
    def agent_runs_get(
        run_id: Annotated[UUID, Path(alias="runId")],
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> Response | AgentRunDetailsResponseDto:
        try:
            runtime = runtime_provider()
            run = get_run_metadata(None, run_id=str(run_id), dependencies=runtime.dependencies)
        except Exception as error:
            return _problem(error)
        capability_guard(principal, AGENT_RUN_READ_CAPABILITY, run.workspace_id)
        try:
            view = get_run(None, run_id=str(run_id), dependencies=runtime.dependencies)
        except Exception as error:
            return _problem(error)
        current_attempt = next(
            (item for item in view.attempts if item.id == view.run.latest_attempt_id),
            None,
        )
        if current_attempt is None:
            return problem_response(503, "Agent service unavailable")
        return _json(
            AgentRunDetailsResponseDto.from_domain(view),
            status_code=200,
            revision=current_attempt.revision,
        )

    @router.get(
        "/api/v1/agent-runs/{runId}/events",
        operation_id="agent_run_events_list",
        response_model=CanonicalEventPageResponseDto,
        responses=_RESPONSES,
    )
    def agent_run_events_list(
        run_id: Annotated[UUID, Path(alias="runId")],
        principal: Annotated[Any, Depends(principal_provider)],
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Response | CanonicalEventPageResponseDto:
        try:
            runtime = runtime_provider()
            run = get_run_metadata(None, run_id=str(run_id), dependencies=runtime.dependencies)
        except Exception as error:
            return _problem(error)
        capability_guard(principal, AGENT_RUN_READ_CAPABILITY, run.workspace_id)
        try:
            page = list_events(
                None,
                run_id=str(run_id),
                cursor=cursor,
                limit=limit,
                dependencies=runtime.dependencies,
            )
        except Exception as error:
            return _problem(error)
        return CanonicalEventPageResponseDto.from_domain(page)

    def control(
        *,
        operation: str,
        run_id: UUID,
        attempt_id: UUID,
        principal: Any,
        preflight: _VersionedPreflight,
    ) -> Response:
        try:
            runtime = runtime_provider()
            run = get_run_metadata(None, run_id=str(run_id), dependencies=runtime.dependencies)
        except Exception as error:
            return _problem(error)
        capability_guard(principal, AGENT_RUN_CONTROL_CAPABILITY, run.workspace_id)
        dependencies, actor = _request_dependencies(runtime, principal)
        if operation == "resume":
            dependencies = dependencies.with_requirement_context(
                LazyRequirementExecutionContext(
                    runtime.requirement_engine, runtime.requirement_context_factory
                )
            )
        correlation_id = current_request_id() or runtime.dependencies.new_id()
        try:
            result = (
                cancel_attempt(
                    None,
                    command=CancelAttemptCommand(
                        run_id=str(run_id),
                        attempt_id=str(attempt_id),
                        expected_revision=preflight.expected_revision,
                        actor=actor,
                        idempotency_key=preflight.idempotency_key,
                        correlation_id=correlation_id,
                    ),
                    dependencies=dependencies,
                )
                if operation == "cancel"
                else resume_attempt(
                    None,
                    command=ResumeAttemptCommand(
                        run_id=str(run_id),
                        attempt_id=str(attempt_id),
                        expected_revision=preflight.expected_revision,
                        actor=actor,
                        idempotency_key=preflight.idempotency_key,
                        correlation_id=correlation_id,
                    ),
                    dependencies=dependencies,
                )
            )
        except Exception as error:
            return _problem(error)
        return _json(
            AttemptControlResponseDto.from_domain(result),
            status_code=202,
            revision=result.attempt.revision,
        )

    @router.post(
        "/api/v1/agent-runs/{runId}/attempts/{attemptId}/cancel",
        operation_id="agent_attempt_cancel",
        status_code=202,
        response_model=AttemptControlResponseDto,
        responses={**_RESPONSES, 202: {"headers": _ATTEMPT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def agent_attempt_cancel(
        run_id: Annotated[UUID, Path(alias="runId")],
        attempt_id: Annotated[UUID, Path(alias="attemptId")],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
        body: AttemptControlRequestDto | None = None,
    ) -> Response:
        return control(
            operation="cancel",
            run_id=run_id,
            attempt_id=attempt_id,
            principal=principal,
            preflight=preflight,
        )

    @router.post(
        "/api/v1/agent-runs/{runId}/attempts/{attemptId}/resume",
        operation_id="agent_attempt_resume",
        status_code=202,
        response_model=AttemptControlResponseDto,
        responses={**_RESPONSES, 202: {"headers": _ATTEMPT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def agent_attempt_resume(
        run_id: Annotated[UUID, Path(alias="runId")],
        attempt_id: Annotated[UUID, Path(alias="attemptId")],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
        body: AttemptControlRequestDto | None = None,
    ) -> Response:
        return control(
            operation="resume",
            run_id=run_id,
            attempt_id=attempt_id,
            principal=principal,
            preflight=preflight,
        )

    return router
