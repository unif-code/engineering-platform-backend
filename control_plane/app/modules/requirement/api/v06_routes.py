from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, Query, Response

from control_plane.app.modules.requirement import (
    ArtifactEvidenceReference,
    confirm_requirement_acceptance,
    decide_formal_review,
    decide_requirement_acceptance,
    reassign_delivery_gate,
    request_formal_merge,
    request_formal_merge_request,
    request_integration_baseline,
    select_integration_baseline,
    submit_external_validation,
)
from control_plane.app.modules.requirement.api.dto import (
    AcceptanceConfirmationRequestDto,
    AcceptanceConfirmationResponseDto,
    AcceptanceDecisionResponseDto,
    DeliveryDecisionRequestDto,
    FormalDeliveryCommandRequestDto,
    FormalDeliveryCommandResponseDto,
    RequestIntegrationBaselineRequestDto,
    RequestIntegrationBaselineResponseDto,
    SelectIntegrationBaselineRequestDto,
    SelectIntegrationBaselineResponseDto,
    SubmitExternalValidationRequestDto,
    SubmitExternalValidationResponseDto,
)
from control_plane.app.modules.requirement.api.problems import (
    V06_PROBLEM_RESPONSES,
)
from control_plane.app.modules.requirement.api.problems import (
    requirement_problem_response as _problem,
)
from control_plane.app.modules.requirement.api.routes import (
    _GATE_ETAG_HEADER,
    _REQUIREMENT_ETAG_HEADER,
    REQUIREMENT_READ_CAPABILITY,
    RequirementHttpRuntime,
    _assert_versioned_preflight,
    _authorized_details,
    _json,
    _versioned_preflight,
    _VersionedPreflight,
)
from control_plane.app.modules.requirement.api.v06_delivery_dto import (
    DeliveryGateReassignmentResponseDto,
    DeliveryHistoryPageResponseDto,
    IntegrationBaselineEvidenceResponseDto,
    ReassignDeliveryGateRequestDto,
    RequirementDeliveryProjectionResponseDto,
)
from control_plane.app.modules.requirement.application.delivery_queries import (
    get_delivery_snapshot_evidence,
    get_requirement_delivery,
    list_requirement_delivery_history,
)

WORK_ITEM_VALIDATION_SUBMIT_CAPABILITY = "work_item.validation.submit"
REQUIREMENT_EVIDENCE_REQUEST_CAPABILITY = "requirement.evidence.request"
REQUIREMENT_EVIDENCE_SELECT_CAPABILITY = "requirement.evidence.select"
REQUIREMENT_ACCEPTANCE_SUBMIT_CAPABILITY = "requirement.acceptance.submit"
REQUIREMENT_ACCEPTANCE_DECIDE_CAPABILITY = "requirement.acceptance.decide"
REQUIREMENT_DELIVERY_GATE_ASSIGN_CAPABILITY = "requirement.delivery_gate.assign"
FORMAL_MERGE_REQUEST_REQUEST_CAPABILITY = "formal_merge_request.request"
FORMAL_MERGE_REQUEST_REVIEW_CAPABILITY = "merge_request.review"
FORMAL_MERGE_REQUEST_MERGE_CAPABILITY = "merge_request.merge"


def create_requirement_v06_delivery_router(
    runtime_provider: Callable[[], RequirementHttpRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    """Default production Artifact, Acceptance and Formal Delivery HTTP boundary."""

    router = APIRouter(prefix="/api/v1/requirements", tags=["requirement-v06"])
    preflight_dependencies = [
        Depends(_assert_versioned_preflight),
        Depends(_versioned_preflight),
    ]

    def authorize(
        runtime: RequirementHttpRuntime,
        principal: Any,
        requirement_id: UUID,
        capability: str,
    ) -> Response | None:
        details = _authorized_details(
            runtime,
            principal,
            str(requirement_id),
            capability,
            capability_guard,
        )
        return details if isinstance(details, Response) else None

    @router.post(
        "/{requirementId}/delivery-gates/{gateId}:reassign",
        operation_id="requirements_reassign_delivery_gate",
        response_model=DeliveryGateReassignmentResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 200: {"headers": _GATE_ETAG_HEADER}},
        dependencies=preflight_dependencies,
    )
    def reassign_gate(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        gate_id: Annotated[UUID, Path(alias="gateId")],
        body: ReassignDeliveryGateRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        if denied := authorize(
            runtime, principal, requirement_id, REQUIREMENT_DELIVERY_GATE_ASSIGN_CAPABILITY
        ):
            return denied
        try:
            with runtime.engine.begin() as db:
                result = reassign_delivery_gate(
                    db,
                    requirement_id=str(requirement_id),
                    gate_id=str(gate_id),
                    candidate_id=body.candidate_id,
                    reason=body.reason,
                    expected_gate_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            DeliveryGateReassignmentResponseDto.from_domain(result),
            status_code=200,
            revision=result.gate.revision,
        )

    @router.post(
        "/{requirementId}/work-items/{workItemId}/external-validations",
        operation_id="requirements_submit_external_validation",
        response_model=SubmitExternalValidationResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=preflight_dependencies,
    )
    def submit_validation(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        work_item_id: Annotated[UUID, Path(alias="workItemId")],
        body: SubmitExternalValidationRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        if denied := authorize(
            runtime,
            principal,
            requirement_id,
            WORK_ITEM_VALIDATION_SUBMIT_CAPABILITY,
        ):
            return denied
        try:
            with runtime.engine.begin() as db:
                result = submit_external_validation(
                    db,
                    requirement_id=str(requirement_id),
                    work_item_id=str(work_item_id),
                    target_commit_sha=body.target_commit_sha,
                    integration_merge_commit_sha=body.integration_merge_commit_sha,
                    reference=body.reference,
                    notes=body.notes,
                    artifact_references=tuple(
                        ArtifactEvidenceReference(
                            artifact_id=item.artifact_id,
                            artifact_version=item.artifact_version,
                            artifact_hash=item.artifact_hash,
                        )
                        for item in body.artifact_references
                    ),
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            SubmitExternalValidationResponseDto.from_domain(result),
            status_code=200,
            revision=result.requirement.revision,
        )

    @router.post(
        "/{requirementId}:request-integration-baseline",
        operation_id="requirements_request_integration_baseline",
        status_code=202,
        response_model=RequestIntegrationBaselineResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 202: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=preflight_dependencies,
    )
    def request_baseline(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: RequestIntegrationBaselineRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        if denied := authorize(
            runtime,
            principal,
            requirement_id,
            REQUIREMENT_EVIDENCE_REQUEST_CAPABILITY,
        ):
            return denied
        try:
            with runtime.engine.begin() as db:
                result = request_integration_baseline(
                    db,
                    requirement_id=str(requirement_id),
                    expected_revision=preflight.expected_revision,
                    expected_requirement_version=body.expected_requirement_version,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            RequestIntegrationBaselineResponseDto.from_domain(result),
            status_code=202,
            revision=result.requirement.revision,
        )

    @router.post(
        "/{requirementId}/integration-baseline-selections",
        operation_id="requirements_select_integration_baseline",
        response_model=SelectIntegrationBaselineResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=preflight_dependencies,
    )
    def select_baseline(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: SelectIntegrationBaselineRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        if denied := authorize(
            runtime,
            principal,
            requirement_id,
            REQUIREMENT_EVIDENCE_SELECT_CAPABILITY,
        ):
            return denied
        try:
            with runtime.engine.begin() as db:
                result = select_integration_baseline(
                    db,
                    requirement_id=str(requirement_id),
                    delivery_snapshot_id=str(body.delivery_snapshot_id),
                    integration_baseline_id=str(body.integration_baseline_id),
                    expected_revision=preflight.expected_revision,
                    expected_requirement_version=body.expected_requirement_version,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            SelectIntegrationBaselineResponseDto.from_domain(result),
            status_code=200,
            revision=result.requirement.revision,
        )

    @router.post(
        "/{requirementId}/acceptance-confirmations",
        operation_id="requirements_confirm_acceptance",
        response_model=AcceptanceConfirmationResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=preflight_dependencies,
    )
    def confirm_acceptance(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: AcceptanceConfirmationRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        runtime = runtime_provider()
        if denied := authorize(
            runtime,
            principal,
            requirement_id,
            REQUIREMENT_ACCEPTANCE_SUBMIT_CAPABILITY,
        ):
            return denied
        try:
            with runtime.engine.begin() as db:
                result = confirm_requirement_acceptance(
                    db,
                    requirement_id=str(requirement_id),
                    selection_id=str(body.selection_id),
                    expected_revision=preflight.expected_revision,
                    actor=principal,
                    idempotency_key=preflight.idempotency_key,
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            AcceptanceConfirmationResponseDto.from_domain(result),
            status_code=200,
            revision=result.requirement.revision,
        )

    def decide(
        requirement_id: UUID,
        body: DeliveryDecisionRequestDto,
        principal: Any,
        preflight: _VersionedPreflight,
        *,
        formal: bool,
    ) -> Response:
        runtime = runtime_provider()
        capability = (
            FORMAL_MERGE_REQUEST_REVIEW_CAPABILITY
            if formal
            else REQUIREMENT_ACCEPTANCE_DECIDE_CAPABILITY
        )
        if denied := authorize(runtime, principal, requirement_id, capability):
            return denied
        try:
            with runtime.engine.begin() as db:
                command = decide_formal_review if formal else decide_requirement_acceptance
                result = command(
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
            AcceptanceDecisionResponseDto.from_domain(result),
            status_code=200,
            revision=result.requirement.revision,
        )

    @router.post(
        "/{requirementId}/acceptance-decisions",
        operation_id="requirements_decide_acceptance",
        response_model=AcceptanceDecisionResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=preflight_dependencies,
    )
    def decide_acceptance(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: DeliveryDecisionRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        return decide(requirement_id, body, principal, preflight, formal=False)

    @router.post(
        "/{requirementId}/formal-review-decisions",
        operation_id="requirements_decide_formal_review",
        response_model=AcceptanceDecisionResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=preflight_dependencies,
    )
    def decide_review(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        body: DeliveryDecisionRequestDto,
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
    ) -> Response:
        return decide(requirement_id, body, principal, preflight, formal=True)

    def request_formal(
        requirement_id: UUID,
        work_item_id: UUID,
        principal: Any,
        preflight: _VersionedPreflight,
        *,
        merge: bool,
    ) -> Response:
        runtime = runtime_provider()
        capability = (
            FORMAL_MERGE_REQUEST_MERGE_CAPABILITY
            if merge
            else FORMAL_MERGE_REQUEST_REQUEST_CAPABILITY
        )
        if denied := authorize(runtime, principal, requirement_id, capability):
            return denied
        try:
            with runtime.engine.begin() as db:
                command = request_formal_merge if merge else request_formal_merge_request
                result = command(
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
            FormalDeliveryCommandResponseDto.from_domain(result),
            status_code=202,
            revision=result.requirement.revision,
        )

    @router.post(
        "/{requirementId}/work-items/{workItemId}:request-formal-mr",
        operation_id="requirements_request_formal_merge_request",
        status_code=202,
        response_model=FormalDeliveryCommandResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 202: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=preflight_dependencies,
    )
    def request_formal_mr(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        work_item_id: Annotated[UUID, Path(alias="workItemId")],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
        body: Annotated[FormalDeliveryCommandRequestDto | None, Body()] = None,
    ) -> Response:
        del body
        return request_formal(
            requirement_id,
            work_item_id,
            principal,
            preflight,
            merge=False,
        )

    @router.post(
        "/{requirementId}/work-items/{workItemId}:request-formal-merge",
        operation_id="requirements_request_formal_merge",
        status_code=202,
        response_model=FormalDeliveryCommandResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 202: {"headers": _REQUIREMENT_ETAG_HEADER}},
        dependencies=preflight_dependencies,
    )
    def request_formal_merge_endpoint(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        work_item_id: Annotated[UUID, Path(alias="workItemId")],
        principal: Annotated[Any, Depends(principal_provider)],
        preflight: Annotated[_VersionedPreflight, Depends(_versioned_preflight)],
        body: Annotated[FormalDeliveryCommandRequestDto | None, Body()] = None,
    ) -> Response:
        del body
        return request_formal(
            requirement_id,
            work_item_id,
            principal,
            preflight,
            merge=True,
        )

    @router.get(
        "/{requirementId}/delivery-snapshots/{snapshotId}/integration-baseline",
        operation_id="requirements_get_delivery_snapshot_evidence",
        response_model=IntegrationBaselineEvidenceResponseDto,
        responses=V06_PROBLEM_RESPONSES,
    )
    def get_snapshot_evidence(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        snapshot_id: Annotated[UUID, Path(alias="snapshotId")],
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> IntegrationBaselineEvidenceResponseDto | Response:
        runtime = runtime_provider()
        if denied := authorize(runtime, principal, requirement_id, REQUIREMENT_READ_CAPABILITY):
            return denied
        try:
            with runtime.engine.connect() as db:
                evidence = get_delivery_snapshot_evidence(
                    runtime.dependencies.repository_factory(db),
                    requirement_id=str(requirement_id),
                    snapshot_id=str(snapshot_id),
                    dependencies=runtime.dependencies,
                )
        except Exception as error:
            return _problem(error)
        return IntegrationBaselineEvidenceResponseDto.model_validate(evidence.model_dump())

    @router.get(
        "/{requirementId}/delivery",
        operation_id="requirements_get_delivery",
        response_model=RequirementDeliveryProjectionResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
    )
    def get_delivery(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> Response:
        runtime = runtime_provider()
        if denied := authorize(runtime, principal, requirement_id, REQUIREMENT_READ_CAPABILITY):
            return denied
        try:
            with runtime.engine.connect() as db:
                projection = get_requirement_delivery(
                    runtime.dependencies.repository_factory(db),
                    requirement_id=str(requirement_id),
                )
        except Exception as error:
            return _problem(error)
        return _json(
            RequirementDeliveryProjectionResponseDto.from_domain(projection),
            status_code=200,
            revision=projection.requirement.revision,
        )

    @router.get(
        "/{requirementId}/delivery/history",
        operation_id="requirements_list_delivery_history",
        response_model=DeliveryHistoryPageResponseDto,
        responses={**V06_PROBLEM_RESPONSES, 200: {"headers": _REQUIREMENT_ETAG_HEADER}},
    )
    def list_delivery_history(
        requirement_id: Annotated[UUID, Path(alias="requirementId")],
        principal: Annotated[Any, Depends(principal_provider)],
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> Response:
        runtime = runtime_provider()
        if denied := authorize(runtime, principal, requirement_id, REQUIREMENT_READ_CAPABILITY):
            return denied
        try:
            with runtime.engine.connect() as db:
                page = list_requirement_delivery_history(
                    runtime.dependencies.repository_factory(db),
                    requirement_id=str(requirement_id),
                    cursor=cursor,
                    limit=limit,
                )
        except Exception as error:
            return _problem(error)
        return _json(
            DeliveryHistoryPageResponseDto.from_domain(page),
            status_code=200,
            revision=page.requirement_revision,
        )

    return router
