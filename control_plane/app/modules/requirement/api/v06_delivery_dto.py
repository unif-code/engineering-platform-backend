from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from control_plane.app.modules.requirement import DeliveryGateReassignmentResult
from control_plane.app.modules.requirement.api.dto import (
    ArtifactEvidenceReferenceResponseDto,
    DeliveryDecisionResponseDto,
    DeliveryGateAssignmentResponseDto,
    DeliveryGateResponseDto,
    IntegrationBaselineSelectionResponseDto,
    RequirementDeliverySnapshotResponseDto,
    RequirementResponseDto,
    StrictCamelModel,
    WorkItemResponseDto,
)
from control_plane.app.modules.requirement.domain import (
    CurrentDeliveryGateProjection,
    DeliveryDecisionDto,
    DeliveryGateAssignmentDto,
    DeliveryGateDto,
    DeliveryHistoryFactType,
    DeliveryHistoryItem,
    DeliveryHistoryPage,
    IntegrationBaselineSelectionDto,
    RequirementDeliveryProjection,
    RequirementDeliverySnapshot,
    WorkItemFormalDeliveryProjection,
)
from control_plane.app.shared.api.camel import CamelModel


class IntegrationBaselineEvidenceWorkItemResponseDto(CamelModel):
    work_item_id: str
    repository_id: str
    task_commit_sha: str
    integration_merge_commit_sha: str
    artifact_references: list[ArtifactEvidenceReferenceResponseDto]


class IntegrationBaselineEvidenceResponseDto(CamelModel):
    id: str
    evidence_hash: str
    delivery_snapshot_id: str
    delivery_snapshot_hash: str
    requirement_id: str
    requirement_version: int
    required_work_item_set_version: int
    required_work_item_set_hash: str
    currentness_state: Literal["CURRENT", "STALE", "UNAVAILABLE"]
    currentness_reasons: list[str]
    work_items: list[IntegrationBaselineEvidenceWorkItemResponseDto]
    generated_at: datetime


class ReassignDeliveryGateRequestDto(StrictCamelModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class DeliveryGateReassignmentResponseDto(CamelModel):
    gate: DeliveryGateResponseDto
    assignment: DeliveryGateAssignmentResponseDto

    @classmethod
    def from_domain(
        cls, value: DeliveryGateReassignmentResult
    ) -> "DeliveryGateReassignmentResponseDto":
        return cls(
            gate=DeliveryGateResponseDto.from_domain(value.gate),
            assignment=DeliveryGateAssignmentResponseDto.from_domain(value.assignment),
        )


class CurrentDeliveryGateProjectionResponseDto(CamelModel):
    gate: DeliveryGateResponseDto
    assignment: DeliveryGateAssignmentResponseDto | None
    decision: DeliveryDecisionResponseDto | None

    @classmethod
    def from_domain(
        cls,
        value: CurrentDeliveryGateProjection,
    ) -> "CurrentDeliveryGateProjectionResponseDto":
        return cls(
            gate=DeliveryGateResponseDto.from_domain(value.gate),
            assignment=(
                None
                if value.assignment is None
                else DeliveryGateAssignmentResponseDto.from_domain(value.assignment)
            ),
            decision=(
                None
                if value.decision is None
                else DeliveryDecisionResponseDto.from_domain(value.decision)
            ),
        )


class WorkItemFormalDeliveryProjectionResponseDto(CamelModel):
    work_item: WorkItemResponseDto
    current_formal_review: CurrentDeliveryGateProjectionResponseDto | None

    @classmethod
    def from_domain(
        cls,
        value: WorkItemFormalDeliveryProjection,
    ) -> "WorkItemFormalDeliveryProjectionResponseDto":
        return cls(
            work_item=WorkItemResponseDto.from_domain(value.work_item),
            current_formal_review=(
                None
                if value.current_formal_review is None
                else CurrentDeliveryGateProjectionResponseDto.from_domain(
                    value.current_formal_review
                )
            ),
        )


class RequirementDeliveryProjectionResponseDto(CamelModel):
    requirement: RequirementResponseDto
    current_delivery_snapshot: RequirementDeliverySnapshotResponseDto | None
    current_selection: IntegrationBaselineSelectionResponseDto | None
    current_acceptance: CurrentDeliveryGateProjectionResponseDto | None
    work_items: list[WorkItemFormalDeliveryProjectionResponseDto]

    @classmethod
    def from_domain(
        cls,
        value: RequirementDeliveryProjection,
    ) -> "RequirementDeliveryProjectionResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            current_delivery_snapshot=(
                None
                if value.current_delivery_snapshot is None
                else RequirementDeliverySnapshotResponseDto.from_domain(
                    value.current_delivery_snapshot
                )
            ),
            current_selection=(
                None
                if value.current_selection is None
                else IntegrationBaselineSelectionResponseDto.from_domain(value.current_selection)
            ),
            current_acceptance=(
                None
                if value.current_acceptance is None
                else CurrentDeliveryGateProjectionResponseDto.from_domain(value.current_acceptance)
            ),
            work_items=[
                WorkItemFormalDeliveryProjectionResponseDto.from_domain(item)
                for item in value.work_items
            ],
        )


DeliveryHistoryFactResponseDto = (
    RequirementDeliverySnapshotResponseDto
    | IntegrationBaselineSelectionResponseDto
    | DeliveryGateResponseDto
    | DeliveryGateAssignmentResponseDto
    | DeliveryDecisionResponseDto
)


class DeliveryHistoryItemResponseDto(CamelModel):
    fact_type: DeliveryHistoryFactType
    occurred_at: datetime
    fact: DeliveryHistoryFactResponseDto

    @classmethod
    def from_domain(cls, value: DeliveryHistoryItem) -> "DeliveryHistoryItemResponseDto":
        fact = value.fact
        if isinstance(fact, RequirementDeliverySnapshot):
            response: DeliveryHistoryFactResponseDto = (
                RequirementDeliverySnapshotResponseDto.from_domain(fact)
            )
        elif isinstance(fact, IntegrationBaselineSelectionDto):
            response = IntegrationBaselineSelectionResponseDto.from_domain(fact)
        elif isinstance(fact, DeliveryGateDto):
            response = DeliveryGateResponseDto.from_domain(fact)
        elif isinstance(fact, DeliveryGateAssignmentDto):
            response = DeliveryGateAssignmentResponseDto.from_domain(fact)
        elif isinstance(fact, DeliveryDecisionDto):
            response = DeliveryDecisionResponseDto.from_domain(fact)
        else:
            raise TypeError(f"unsupported Requirement delivery history fact: {type(fact)!r}")
        return cls(fact_type=value.fact_type, occurred_at=value.occurred_at, fact=response)


class DeliveryHistoryPageResponseDto(CamelModel):
    requirement_id: UUID
    requirement_revision: int
    items: list[DeliveryHistoryItemResponseDto]
    next_cursor: str | None

    @classmethod
    def from_domain(cls, value: DeliveryHistoryPage) -> "DeliveryHistoryPageResponseDto":
        return cls(
            requirement_id=UUID(value.requirement_id),
            requirement_revision=value.requirement_revision,
            items=[DeliveryHistoryItemResponseDto.from_domain(item) for item in value.items],
            next_cursor=value.next_cursor,
        )
