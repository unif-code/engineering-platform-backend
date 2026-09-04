from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.requirement.domain.acceptance import (
    DeliveryDecisionDto,
    DeliveryGateAssignmentDto,
    DeliveryGateDto,
    IntegrationBaselineSelectionDto,
)
from control_plane.app.modules.requirement.domain.evidence import RequirementDeliverySnapshot
from control_plane.app.modules.requirement.domain.models import RequirementDto, WorkItemDto


class DeliveryHistoryFactType(StrEnum):
    DELIVERY_SNAPSHOT = "DELIVERY_SNAPSHOT"
    INTEGRATION_BASELINE_SELECTION = "INTEGRATION_BASELINE_SELECTION"
    DELIVERY_GATE = "DELIVERY_GATE"
    DELIVERY_GATE_ASSIGNMENT = "DELIVERY_GATE_ASSIGNMENT"
    DELIVERY_DECISION = "DELIVERY_DECISION"


DeliveryHistoryFact = (
    RequirementDeliverySnapshot
    | IntegrationBaselineSelectionDto
    | DeliveryGateDto
    | DeliveryGateAssignmentDto
    | DeliveryDecisionDto
)


class CurrentDeliveryGateProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate: DeliveryGateDto
    assignment: DeliveryGateAssignmentDto | None
    decision: DeliveryDecisionDto | None


class WorkItemFormalDeliveryProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item: WorkItemDto
    current_formal_review: CurrentDeliveryGateProjection | None


class RequirementDeliveryProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    current_delivery_snapshot: RequirementDeliverySnapshot | None
    current_selection: IntegrationBaselineSelectionDto | None
    current_acceptance: CurrentDeliveryGateProjection | None
    work_items: tuple[WorkItemFormalDeliveryProjection, ...]


class DeliveryHistoryItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact_type: DeliveryHistoryFactType
    occurred_at: datetime
    fact: DeliveryHistoryFact


class DeliveryHistoryPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement_id: str
    requirement_revision: int
    items: tuple[DeliveryHistoryItem, ...]
    next_cursor: str | None
