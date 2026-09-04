from dataclasses import dataclass
from typing import Any, cast

from fastapi import Response
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.requirement.application.delivery import WorkItemActorDenied
from control_plane.app.modules.requirement.domain import (
    AcceptanceStale,
    ArtifactUnavailable,
    DeliverySnapshotConflict,
    EvidenceUnavailableOrStale,
    FormalDeliveryBlocked,
    FormalReviewStale,
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
    SelectionStale,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    WorkItemNotFound,
)
from control_plane.app.shared.api.camel import CamelModel
from control_plane.app.shared.api.problem import problem_response
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
)
from control_plane.app.shared.security import SecretMaterialUnavailable


class RequirementProblem(CamelModel):
    type: str | None = None
    title: str
    status: int
    detail: str | None = None
    reason: str | None = None
    request_id: str | None = None


V06_PROBLEM_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        status: {
            "description": description,
            "content": {
                "application/problem+json": {
                    "schema": RequirementProblem.model_json_schema(by_alias=True),
                }
            },
        }
        for status, description in {
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            409: "Conflict",
            422: "Validation failed",
            500: "Internal server error",
            503: "Not ready",
        }.items()
    },
)


@dataclass(frozen=True, slots=True)
class _ConflictProblem:
    error_type: type[Exception]
    title: str
    type_uri: str
    reason: str

    def response(self) -> Response:
        return problem_response(
            409,
            self.title,
            extra={"type": self.type_uri, "reason": self.reason},
        )


_V06_CONFLICTS = (
    _ConflictProblem(
        StaleRequirementRevision,
        "Requirement snapshot conflict",
        "urn:engineering-platform:problem:requirement:snapshot-conflict",
        "SNAPSHOT_CONFLICT",
    ),
    _ConflictProblem(
        StaleWorkItemRevision,
        "Requirement snapshot conflict",
        "urn:engineering-platform:problem:requirement:snapshot-conflict",
        "SNAPSHOT_CONFLICT",
    ),
    _ConflictProblem(
        DeliverySnapshotConflict,
        "Requirement snapshot conflict",
        "urn:engineering-platform:problem:requirement:snapshot-conflict",
        "SNAPSHOT_CONFLICT",
    ),
    _ConflictProblem(
        EvidenceUnavailableOrStale,
        "Delivery evidence unavailable or stale",
        "urn:engineering-platform:problem:requirement:evidence-unavailable-or-stale",
        "EVIDENCE_UNAVAILABLE_OR_STALE",
    ),
    _ConflictProblem(
        SelectionStale,
        "Integration baseline selection stale",
        "urn:engineering-platform:problem:requirement:selection-stale",
        "SELECTION_STALE",
    ),
    _ConflictProblem(
        AcceptanceStale,
        "Requirement acceptance stale",
        "urn:engineering-platform:problem:requirement:acceptance-stale",
        "ACCEPTANCE_STALE",
    ),
    _ConflictProblem(
        FormalReviewStale,
        "Formal review stale",
        "urn:engineering-platform:problem:requirement:review-stale",
        "REVIEW_STALE",
    ),
    _ConflictProblem(
        FormalDeliveryBlocked,
        "Formal delivery blocked",
        "urn:engineering-platform:problem:requirement:formal-delivery-blocked",
        "FORMAL_DELIVERY_BLOCKED",
    ),
)


def requirement_problem_response(error: Exception) -> Response:
    """Render the stable public Requirement error contract without leaking details."""

    for conflict in _V06_CONFLICTS:
        if isinstance(error, conflict.error_type):
            return conflict.response()
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
