from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.requirement.domain.models import (
    DecisionOutcome,
    RequirementDto,
)
from control_plane.app.modules.requirement.domain.transitions import RequirementError


class SelectionStale(RequirementError):
    pass


class AcceptanceStale(RequirementError):
    pass


class DeliveryGateType(StrEnum):
    REQUIREMENT_ACCEPTANCE = "REQUIREMENT_ACCEPTANCE"
    FORMAL_MR_REVIEW = "FORMAL_MR_REVIEW"


class DeliveryGateState(StrEnum):
    OPEN = "OPEN"
    DECIDED = "DECIDED"
    INVALIDATED = "INVALIDATED"


class DecisionValidity(StrEnum):
    CURRENT = "CURRENT"
    INVALIDATED = "INVALIDATED"


class IntegrationBaselineSelectionDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    requirement_id: str
    delivery_snapshot_id: str
    integration_baseline_id: str
    integration_baseline_hash: str
    evidence_requirement_version: int = Field(ge=1)
    evidence_required_work_item_set_version: int = Field(ge=1)
    evidence_required_work_item_set_hash: str
    requirement_version_before: int = Field(ge=1)
    requirement_version_after: int = Field(ge=2)
    selected_by: str
    selected_at: datetime
    invalidated_at: datetime | None
    invalidation_reason: str | None


class DeliveryGateDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    gate_type: DeliveryGateType
    requirement_id: str
    work_item_id: str | None
    selection_id: str
    requirement_version: int = Field(ge=1)
    acceptance_criteria_version: int = Field(ge=1)
    acceptance_criteria_hash: str
    integration_baseline_id: str
    integration_baseline_hash: str
    formal_merge_request_binding_id: str | None
    subject_head_sha: str | None
    policy_code: str
    policy_version: int = Field(ge=1)
    policy_snapshot_hash: str
    state: DeliveryGateState
    revision: int = Field(ge=1)
    created_at: datetime
    decided_at: datetime | None
    invalidated_at: datetime | None
    invalidation_reason: str | None


class DeliveryGateAssignmentDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    gate_id: str
    default_reviewer_id: str
    current_reviewer_id: str
    resolution_snapshot: dict[str, object]
    revision: int = Field(ge=1)
    assigned_at: datetime
    superseded_at: datetime | None


class DeliveryDecisionDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    gate_id: str
    gate_assignment_id: str
    reviewer_id: str
    outcome: DecisionOutcome
    reason: str
    subject_revision: int = Field(ge=1)
    requirement_version: int = Field(ge=1)
    acceptance_criteria_version: int = Field(ge=1)
    acceptance_criteria_hash: str
    integration_baseline_id: str
    integration_baseline_hash: str
    subject_head_sha: str | None
    eligibility_snapshot: dict[str, object]
    validity: DecisionValidity
    decided_at: datetime
    invalidated_at: datetime | None
    invalidation_reason: str | None


class DeliveryGateReassignmentResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    gate: DeliveryGateDto
    assignment: DeliveryGateAssignmentDto


class SelectIntegrationBaselineResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    selection: IntegrationBaselineSelectionDto
    outbox_topic: str


class AcceptanceConfirmationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    selection: IntegrationBaselineSelectionDto
    gate: DeliveryGateDto
    assignment: DeliveryGateAssignmentDto


class AcceptanceDecisionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    selection: IntegrationBaselineSelectionDto
    gate: DeliveryGateDto
    assignment: DeliveryGateAssignmentDto
    decision: DeliveryDecisionDto


class CurrentAcceptanceProof(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement_id: str
    current: bool
    requirement_version: int
    selection_id: str | None = None
    gate_id: str | None = None
    decision_id: str | None = None
    integration_baseline_id: str | None = None
    integration_baseline_hash: str | None = None
    outcome: DecisionOutcome | None = None
