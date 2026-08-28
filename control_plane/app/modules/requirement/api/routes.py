from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.requirement import (
    add_work_item,
    assign_work_item,
    create_requirement,
    create_sdd_artifact,
    decide_baseline,
    get_requirement,
    get_sdd_artifact,
    list_requirements,
    reassign_baseline_gate,
    register_sdd_baseline,
    request_integration_merge,
    request_integration_merge_request,
    start_work_item,
    submit_baseline_confirmation,
)
from control_plane.app.modules.requirement.api.dto import (
    AddWorkItemRequestDto,
    AddWorkItemResponseDto,
    AssignWorkItemRequestDto,
    AssignWorkItemResponseDto,
    BaselineConfirmationResponseDto,
    BaselineDecisionResponseDto,
    CreateRequirementRequestDto,
    CreateRequirementResponseDto,
    CreateSddArtifactRequestDto,
    CreateSddArtifactResponseDto,
    DecideBaselineRequestDto,
    GateReassignmentResponseDto,
    ReassignBaselineGateRequestDto,
    RegisterSddBaselineRequestDto,
    RegisterSddBaselineResponseDto,
    RequirementDetailsResponseDto,
    RequirementListResponseDto,
    SddArtifactVersionResponseDto,
    SubmitBaselineConfirmationRequestDto,
    WorkItemDeliveryCommandRequestDto,
    WorkItemDeliveryResponseDto,
)
from control_plane.app.modules.requirement.application import RequirementDependencies
from control_plane.app.modules.requirement.application.delivery import WorkItemActorDenied
from control_plane.app.modules.requirement.domain import (
    ArtifactUnavailable,
    GateNotFound,
    GateReviewerIneligible,
    GateReviewerMismatch,
    InvalidRequirementCursor,
    InvalidRequirementInput,
    RequirementDependencyUnavailable,
    RequirementError,
    RequirementNotFound,
    SddArtifactNotFound,
    SddBaselineNotFound,
    WorkItemNotFound,
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
)
from control_plane.app.shared.security import SecretMaterialUnavailable, assert_same_origin

REQUIREMENT_CREATE_CAPABILITY = "requirement.create"
REQUIREMENT_READ_CAPABILITY = "requirement.read"
REQUIREMENT_BASELINE_SUBMIT_CAPABILITY = "requirement.baseline.submit"
REQUIREMENT_BASELINE_ASSIGN_CAPABILITY = "requirement.baseline.assign"
REQUIREMENT_BASELINE_DECIDE_CAPABILITY = "requirement.baseline.decide"
WORK_ITEM_CREATE_CAPABILITY = "work_item.create"
WORK_ITEM_ASSIGN_CAPABILITY = "work_item.assign"
WORK_ITEM_EXECUTE_CAPABILITY = "work_item.execute"
MERGE_REQUEST_MERGE_CAPABILITY = "merge_request.merge"

_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 409, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)
_REQUIREMENT_ETAG_HEADER = {
    "ETag": {
        "description": "Strong Requirement revision entity tag",
        "schema": {"type": "string", "pattern": '^"v[1-9][0-9]*"$'},
    }
}
_WORK_ITEM_ETAG_HEADER = {
    "ETag": {
        "description": "Strong WorkItem revision entity tag",
        "schema": {"type": "string", "pattern": '^"v[1-9][0-9]*"$'},
    }
}
_GATE_ETAG_HEADER = {
    "ETag": {
        "description": "Strong Gate revision entity tag",
        "schema": {"type": "string", "pattern": '^"v[1-9][0-9]*"$'},
    }
}


@dataclass(frozen=True, slots=True)
class RequirementHttpRuntime:
    engine: Engine
    dependencies: RequirementDependencies


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
    if isinstance(
        error,
        (
            RequirementNotFound,
            WorkItemNotFound,
            SddArtifactNotFound,
            SddBaselineNotFound,
            GateNotFound,
        ),
    ):
        return problem_response(404, "Requirement subject not found")
    if isinstance(error, WorkItemActorDenied):
        return problem_response(403, "WorkItem actor denied")
    if isinstance(error, (GateReviewerMismatch, GateReviewerIneligible)):
        return problem_response(403, "Baseline reviewer denied")
    if isinstance(error, InvalidRequirementInput):
        return problem_response(422, "Invalid Requirement input")
    if isinstance(error, InvalidRequirementCursor):
        return problem_response(422, "Invalid Requirement cursor")
    if isinstance(error, RequirementDependencyUnavailable):
        return problem_response(503, "Requirement dependency unavailable")
    if isinstance(error, ArtifactUnavailable):
        return problem_response(409, "SDD Artifact unavailable")
    if isinstance(error, (IdempotencyConflict, IdempotencyReplayUnavailable)):
        return problem_response(409, "Idempotency conflict")
    if isinstance(error, (SQLAlchemyError, SecretMaterialUnavailable)):
        return problem_response(503, "Requirement service unavailable")
    if isinstance(error, RequirementError):
        return problem_response(409, "Requirement state conflict")
    raise error


def _json(dto: Any, *, status_code: int, revision: int | None = None) -> JSONResponse:
    headers = {} if revision is None else {"ETag": entity_tag(revision)}
    return JSONResponse(
        status_code=status_code,
        content=dto.model_dump(mode="json", by_alias=True),
        headers=headers,
    )


def _authorized_details(
    runtime: RequirementHttpRuntime,
    principal: Any,
    requirement_id: str,
    capability: str,
    capability_guard: Callable[[Any, str, str | None], None],
) -> Any:
    try:
        with runtime.engine.connect() as db:
            details = get_requirement(
                db,
                requirement_id=requirement_id,
                dependencies=runtime.dependencies,
            )
    except Exception as error:
        return _problem(error)
    capability_guard(principal, capability, details.requirement.workspace_id)
    return details


def create_requirement_foundation_router(
    runtime_provider: Callable[[], RequirementHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/requirements", tags=["requirement"])

    @router.post(
        "",
        operation_id="requirements_create",
        status_code=201,
        response_model=CreateRequirementResponseDto,
        responses={**_RESPONSES, 201: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=[Depends(_assert_create_preflight), Depends(_create_preflight)],
    )
    def requirement_create(
        body: CreateRequirementRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_CreatePreflight, Depends(_create_preflight)],
    ) -> Response:
        workspace_id = str(body.workspace_id)
        capability_guard(principal, REQUIREMENT_CREATE_CAPABILITY, workspace_id)
        runtime = runtime_provider()
        try:
            with runtime.engine.begin() as db:
                created = create_requirement(
                    db,
                    workspace_id=workspace_id,
                    requirement_type=body.type,
                    title=body.title,
                    description=body.description,
                    acceptance_criteria=tuple(body.acceptance_criteria),
                    initial_repository_id=body.initial_repository_id,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            CreateRequirementResponseDto.from_domain(created),
            status_code=201,
            revision=created.requirement.revision,
        )

    @router.get(
        "",
        operation_id="requirements_list",
        response_model=RequirementListResponseDto,
        responses=_RESPONSES,
    )
    def requirement_list(
        principal: Annotated[Any, Depends(principal_provider)],
        workspace_id: Annotated[UUID, Query(alias="workspaceId")],
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> RequirementListResponseDto | Response:
        resolved_workspace_id = str(workspace_id)
        capability_guard(principal, REQUIREMENT_READ_CAPABILITY, resolved_workspace_id)
        runtime = runtime_provider()
        try:
            with runtime.engine.connect() as db:
                page = list_requirements(
                    db,
                    workspace_id=resolved_workspace_id,
                    cursor=cursor,
                    limit=limit,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return RequirementListResponseDto.from_domain(page)

    @router.get(
        "/{requirementId}",
        operation_id="requirements_get",
        response_model=RequirementDetailsResponseDto,
        responses={**_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
    )
    def requirement_get(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> Response:
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            REQUIREMENT_READ_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        return _json(
            RequirementDetailsResponseDto.from_domain(details),
            status_code=200,
            revision=details.requirement.revision,
        )

    return router


def create_requirement_planning_router(
    runtime_provider: Callable[[], RequirementHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/requirements", tags=["requirement"])

    @router.post(
        "/{requirementId}/sdd-artifacts",
        operation_id="requirements_create_sdd_artifact",
        status_code=201,
        response_model=CreateSddArtifactResponseDto,
        responses={**_RESPONSES, 201: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_create_sdd_artifact(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: CreateSddArtifactRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            REQUIREMENT_BASELINE_SUBMIT_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                created = create_sdd_artifact(
                    db,
                    requirement_id=str(requirement_id),
                    artifact_id=None if body.artifact_id is None else str(body.artifact_id),
                    content=body.content,
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            CreateSddArtifactResponseDto.from_domain(created),
            status_code=201,
            revision=created.requirement.revision,
        )

    @router.get(
        "/{requirementId}/sdd-artifacts/{artifactId}/versions/{artifactVersion}",
        operation_id="requirements_get_sdd_artifact_version",
        response_model=SddArtifactVersionResponseDto,
        responses=_RESPONSES,
    )
    def requirement_get_sdd_artifact_version(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        artifact_id: Annotated[UUID, Path(alias="artifactId")],
        artifact_version: Annotated[int, Path(alias="artifactVersion", ge=1)],
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> Response:
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            REQUIREMENT_READ_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.connect() as db:
                artifact = get_sdd_artifact(
                    db,
                    requirement_id=str(requirement_id),
                    artifact_id=str(artifact_id),
                    artifact_version=artifact_version,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            SddArtifactVersionResponseDto.from_domain(artifact),
            status_code=200,
        )

    @router.post(
        "/{requirementId}/work-items",
        operation_id="requirements_add_work_item",
        status_code=201,
        response_model=AddWorkItemResponseDto,
        responses={**_RESPONSES, 201: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_add_work_item(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: AddWorkItemRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            WORK_ITEM_CREATE_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                added = add_work_item(
                    db,
                    requirement_id=str(requirement_id),
                    repository_id=body.repository_id,
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            AddWorkItemResponseDto.from_domain(added),
            status_code=201,
            revision=added.requirement.revision,
        )

    @router.post(
        "/{requirementId}/work-items/{workItemId}:assign",
        operation_id="requirements_assign_work_item",
        response_model=AssignWorkItemResponseDto,
        responses={**_RESPONSES, 200: {"headers": _WORK_ITEM_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_assign_work_item(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        work_item_id: Annotated[UUID, Path(alias="workItemId")],
        body: AssignWorkItemRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            WORK_ITEM_ASSIGN_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                assigned = assign_work_item(
                    db,
                    requirement_id=str(requirement_id),
                    work_item_id=str(work_item_id),
                    human_owner_id=body.human_owner_id,
                    reason=body.reason,
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            AssignWorkItemResponseDto.from_domain(assigned),
            status_code=200,
            revision=assigned.work_item.revision,
        )

    return router


def create_requirement_baseline_router(
    runtime_provider: Callable[[], RequirementHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/requirements", tags=["requirement"])

    @router.post(
        "/{requirementId}/sdd-baselines",
        operation_id="requirements_register_sdd_baseline",
        status_code=201,
        response_model=RegisterSddBaselineResponseDto,
        responses={**_RESPONSES, 201: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_register_sdd_baseline(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: RegisterSddBaselineRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            REQUIREMENT_BASELINE_SUBMIT_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                registered = register_sdd_baseline(
                    db,
                    requirement_id=str(requirement_id),
                    artifact_id=body.artifact_id,
                    artifact_version=body.artifact_version,
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            RegisterSddBaselineResponseDto.from_domain(registered),
            status_code=201,
            revision=registered.requirement.revision,
        )

    @router.post(
        "/{requirementId}/baseline-confirmations",
        operation_id="requirements_submit_baseline_confirmation",
        status_code=201,
        response_model=BaselineConfirmationResponseDto,
        responses={**_RESPONSES, 201: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_submit_baseline_confirmation(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: SubmitBaselineConfirmationRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            REQUIREMENT_BASELINE_SUBMIT_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                confirmation = submit_baseline_confirmation(
                    db,
                    requirement_id=str(requirement_id),
                    sdd_baseline_id=str(body.sdd_baseline_id),
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            BaselineConfirmationResponseDto.from_domain(confirmation),
            status_code=201,
            revision=confirmation.requirement.revision,
        )

    @router.post(
        "/{requirementId}/baseline-gates/{gateId}:reassign",
        operation_id="requirements_reassign_baseline_gate",
        response_model=GateReassignmentResponseDto,
        responses={**_RESPONSES, 200: {"headers": _GATE_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_reassign_baseline_gate(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        gate_id: Annotated[UUID, Path(alias="gateId")],
        body: ReassignBaselineGateRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            REQUIREMENT_BASELINE_ASSIGN_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                reassigned = reassign_baseline_gate(
                    db,
                    requirement_id=str(requirement_id),
                    gate_id=str(gate_id),
                    reviewer_id=body.reviewer_id,
                    reason=body.reason,
                    expected_gate_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            GateReassignmentResponseDto.from_domain(reassigned),
            status_code=200,
            revision=reassigned.gate.revision,
        )

    @router.post(
        "/{requirementId}/baseline-decisions",
        operation_id="requirements_decide_baseline",
        response_model=BaselineDecisionResponseDto,
        responses={**_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_decide_baseline(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: DecideBaselineRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            REQUIREMENT_BASELINE_DECIDE_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                decision = decide_baseline(
                    db,
                    requirement_id=str(requirement_id),
                    gate_id=str(body.gate_id),
                    outcome=body.outcome,
                    reason=body.reason,
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            BaselineDecisionResponseDto.from_domain(decision),
            status_code=200,
            revision=decision.requirement.revision,
        )

    return router


def create_requirement_delivery_router(
    runtime_provider: Callable[[], RequirementHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/requirements", tags=["requirement"])

    @router.post(
        "/{requirementId}/work-items/{workItemId}:start",
        operation_id="requirements_start_work_item",
        response_model=WorkItemDeliveryResponseDto,
        responses={**_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_start_work_item(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        work_item_id: Annotated[UUID, Path(alias="workItemId")],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
        body: Annotated[WorkItemDeliveryCommandRequestDto | None, Body()] = None,
    ) -> Response:
        del body
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            WORK_ITEM_EXECUTE_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                result = start_work_item(
                    db,
                    requirement_id=str(requirement_id),
                    work_item_id=str(work_item_id),
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            WorkItemDeliveryResponseDto.from_domain(result),
            status_code=200,
            revision=result.requirement.revision,
        )

    @router.post(
        "/{requirementId}/work-items/{workItemId}:request-integration-mr",
        operation_id="requirements_request_integration_merge_request",
        status_code=202,
        response_model=WorkItemDeliveryResponseDto,
        responses={**_RESPONSES, 202: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_request_integration_merge_request(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        work_item_id: Annotated[UUID, Path(alias="workItemId")],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
        body: Annotated[WorkItemDeliveryCommandRequestDto | None, Body()] = None,
    ) -> Response:
        del body
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            WORK_ITEM_EXECUTE_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                result = request_integration_merge_request(
                    db,
                    requirement_id=str(requirement_id),
                    work_item_id=str(work_item_id),
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            WorkItemDeliveryResponseDto.from_domain(result),
            status_code=202,
            revision=result.requirement.revision,
        )

    @router.post(
        "/{requirementId}/work-items/{workItemId}:request-integration-merge",
        operation_id="requirements_request_integration_merge",
        status_code=202,
        response_model=WorkItemDeliveryResponseDto,
        responses={**_RESPONSES, 202: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=[Depends(_assert_versioned_preflight), Depends(_versioned_preflight)],
    )
    def requirement_request_integration_merge(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        work_item_id: Annotated[UUID, Path(alias="workItemId")],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
        body: Annotated[WorkItemDeliveryCommandRequestDto | None, Body()] = None,
    ) -> Response:
        del body
        runtime = runtime_provider()
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            MERGE_REQUEST_MERGE_CAPABILITY,
            capability_guard,
        )
        if isinstance(details, Response):
            return details
        try:
            with runtime.engine.begin() as db:
                result = request_integration_merge(
                    db,
                    requirement_id=str(requirement_id),
                    work_item_id=str(work_item_id),
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            WorkItemDeliveryResponseDto.from_domain(result),
            status_code=202,
            revision=result.requirement.revision,
        )

    return router
