from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field

from control_plane.app.modules.requirement.application.delivery import (
    WorkItemDeliveryDto,
    WorkItemDeliveryResult,
)
from control_plane.app.modules.requirement.domain import (
    AddWorkItemResult,
    AssignmentState,
    AssignWorkItemResult,
    BaselineConfirmationResult,
    BaselineDecisionResult,
    CreateRequirementResult,
    CreateSddArtifactResult,
    DecisionDto,
    DecisionOutcome,
    ExecutorType,
    GateAssignmentDto,
    GateInstanceDto,
    GateReassignmentResult,
    GateState,
    GateType,
    IntegrationDeliveryBlockedReason,
    IntegrationDeliveryState,
    RecordState,
    RegisterSddBaselineResult,
    RepositoryBindingBlockedReason,
    RepositoryState,
    RequirementDetailsDto,
    RequirementDto,
    RequirementPage,
    RequirementState,
    RequirementType,
    SddArtifactVersionDto,
    SddBaselineDto,
    WorkItemAssignmentDto,
    WorkItemDto,
    WorkItemState,
)
from control_plane.app.shared.api.camel import CamelModel


class StrictCamelModel(CamelModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=False,
        validate_by_alias=True,
        validate_by_name=False,
    )


class CreateRequirementRequestDto(StrictCamelModel):
    workspace_id: UUID
    type: RequirementType
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10000)
    acceptance_criteria: list[str] = Field(min_length=1)
    initial_repository_id: str = Field(min_length=1)


class RegisterSddBaselineRequestDto(StrictCamelModel):
    artifact_id: UUID
    artifact_version: int = Field(strict=True, ge=1)


class CreateSddArtifactRequestDto(StrictCamelModel):
    artifact_id: UUID | None = None
    content: str = Field(min_length=1, max_length=200_000)


class AddWorkItemRequestDto(StrictCamelModel):
    repository_id: str = Field(min_length=1, max_length=200)


class AssignWorkItemRequestDto(StrictCamelModel):
    human_owner_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class ReassignBaselineGateRequestDto(StrictCamelModel):
    reviewer_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class SubmitBaselineConfirmationRequestDto(StrictCamelModel):
    sdd_baseline_id: UUID


class DecideBaselineRequestDto(StrictCamelModel):
    gate_id: UUID
    outcome: DecisionOutcome
    reason: str = Field(min_length=1, max_length=2000)


class WorkItemDeliveryCommandRequestDto(StrictCamelModel):
    pass


class RequirementResponseDto(CamelModel):
    id: UUID
    workspace_id: UUID
    type: RequirementType
    title: str
    description: str
    acceptance_criteria: list[str]
    created_by: str
    initial_repository_id: str
    route_snapshot_version: int
    route_snapshot_hash: str
    route_snapshot: dict[str, object]
    state: RequirementState
    record_state: RecordState
    requirement_version: int
    required_work_item_set_version: int
    required_work_item_set_hash: str
    current_sdd_baseline_id: UUID | None
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: RequirementDto) -> "RequirementResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class WorkItemResponseDto(CamelModel):
    id: UUID
    requirement_id: UUID
    created_by: str
    human_owner_id: str | None
    executor_type: ExecutorType
    executor_id: str | None
    required_capabilities: list[str]
    assignment_state: AssignmentState
    repository_state: RepositoryState
    state: WorkItemState
    repository_id: str
    base_commit_sha: str | None
    task_branch: str | None
    repository_blocked_reason_code: RepositoryBindingBlockedReason | None
    repository_blocked_at: datetime | None
    integration_delivery_state: IntegrationDeliveryState
    integration_merge_request_binding_id: UUID | None
    integration_blocked_reason_code: IntegrationDeliveryBlockedReason | None
    integration_updated_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: WorkItemDto) -> "WorkItemResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class CreateRequirementResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_item: WorkItemResponseDto

    @classmethod
    def from_domain(cls, value: CreateRequirementResult) -> "CreateRequirementResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_item=WorkItemResponseDto.from_domain(value.work_item),
        )


class WorkItemDeliveryResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_item: "WorkItemDeliveryProjectionResponseDto"

    @classmethod
    def from_domain(cls, value: WorkItemDeliveryResult) -> "WorkItemDeliveryResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_item=WorkItemDeliveryProjectionResponseDto.from_domain(value.work_item),
        )


class WorkItemDeliveryProjectionResponseDto(CamelModel):
    id: UUID
    requirement_id: UUID
    human_owner_id: str | None
    assignment_state: AssignmentState
    repository_state: RepositoryState
    state: WorkItemState
    repository_id: str
    integration_delivery_state: IntegrationDeliveryState
    integration_merge_request_binding_id: UUID | None
    integration_blocked_reason_code: IntegrationDeliveryBlockedReason | None
    integration_updated_at: datetime | None
    revision: int

    @classmethod
    def from_domain(cls, value: WorkItemDeliveryDto) -> "WorkItemDeliveryProjectionResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class RequirementListResponseDto(CamelModel):
    items: list[RequirementResponseDto]
    next_cursor: str | None

    @classmethod
    def from_domain(cls, value: RequirementPage) -> "RequirementListResponseDto":
        return cls(
            items=[RequirementResponseDto.from_domain(item) for item in value.items],
            next_cursor=value.next_cursor,
        )


class SddBaselineResponseDto(CamelModel):
    id: UUID
    requirement_id: UUID
    requirement_version: int
    artifact_id: str
    artifact_version: str
    artifact_hash: str
    route_snapshot_version: int
    route_snapshot_hash: str
    created_by: str
    created_at: datetime

    @classmethod
    def from_domain(cls, value: SddBaselineDto) -> "SddBaselineResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class GateInstanceResponseDto(CamelModel):
    id: UUID
    gate_type: GateType
    requirement_id: UUID
    requirement_version: int
    sdd_baseline_id: UUID
    artifact_id: str
    artifact_version: str
    artifact_hash: str
    route_snapshot_version: int
    route_snapshot_hash: str
    policy_code: str
    policy_version: int
    policy_snapshot_hash: str
    state: GateState
    revision: int
    created_at: datetime
    decided_at: datetime | None

    @classmethod
    def from_domain(cls, value: GateInstanceDto) -> "GateInstanceResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class GateAssignmentResponseDto(CamelModel):
    id: UUID
    gate_instance_id: UUID
    default_reviewer_id: str
    current_reviewer_id: str
    revision: int
    assigned_at: datetime
    superseded_at: datetime | None

    @classmethod
    def from_domain(cls, value: GateAssignmentDto) -> "GateAssignmentResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class DecisionResponseDto(CamelModel):
    id: UUID
    gate_instance_id: UUID
    gate_assignment_id: UUID
    reviewer_id: str
    outcome: DecisionOutcome
    reason: str
    subject_revision: int
    decided_at: datetime

    @classmethod
    def from_domain(cls, value: DecisionDto) -> "DecisionResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class RegisterSddBaselineResponseDto(CamelModel):
    requirement: RequirementResponseDto
    baseline: SddBaselineResponseDto

    @classmethod
    def from_domain(cls, value: RegisterSddBaselineResult) -> "RegisterSddBaselineResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            baseline=SddBaselineResponseDto.from_domain(value.baseline),
        )


class BaselineConfirmationResponseDto(CamelModel):
    requirement: RequirementResponseDto
    gate: GateInstanceResponseDto
    assignment: GateAssignmentResponseDto

    @classmethod
    def from_domain(
        cls,
        value: BaselineConfirmationResult,
    ) -> "BaselineConfirmationResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            gate=GateInstanceResponseDto.from_domain(value.gate),
            assignment=GateAssignmentResponseDto.from_domain(value.assignment),
        )


class BaselineDecisionResponseDto(CamelModel):
    requirement: RequirementResponseDto
    gate: GateInstanceResponseDto
    decision: DecisionResponseDto

    @classmethod
    def from_domain(cls, value: BaselineDecisionResult) -> "BaselineDecisionResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            gate=GateInstanceResponseDto.from_domain(value.gate),
            decision=DecisionResponseDto.from_domain(value.decision),
        )


class WorkItemAssignmentResponseDto(CamelModel):
    id: UUID
    work_item_id: UUID
    assignee_id: str
    assigned_by: str
    reason: str
    revision: int
    assigned_at: datetime
    superseded_at: datetime | None

    @classmethod
    def from_domain(cls, value: WorkItemAssignmentDto) -> "WorkItemAssignmentResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class SddArtifactVersionResponseDto(CamelModel):
    artifact_id: UUID
    version: int
    requirement_id: UUID
    sha256: str
    state: str
    media_type: str
    trust: str
    content: str
    created_by: str
    created_at: datetime

    @classmethod
    def from_domain(cls, value: SddArtifactVersionDto) -> "SddArtifactVersionResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class CreateSddArtifactResponseDto(CamelModel):
    requirement: RequirementResponseDto
    artifact: SddArtifactVersionResponseDto

    @classmethod
    def from_domain(cls, value: CreateSddArtifactResult) -> "CreateSddArtifactResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            artifact=SddArtifactVersionResponseDto.from_domain(value.artifact),
        )


class AddWorkItemResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_item: WorkItemResponseDto
    assignment: WorkItemAssignmentResponseDto | None

    @classmethod
    def from_domain(cls, value: AddWorkItemResult) -> "AddWorkItemResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_item=WorkItemResponseDto.from_domain(value.work_item),
            assignment=(
                None
                if value.assignment is None
                else WorkItemAssignmentResponseDto.from_domain(value.assignment)
            ),
        )


class AssignWorkItemResponseDto(CamelModel):
    work_item: WorkItemResponseDto
    assignment: WorkItemAssignmentResponseDto

    @classmethod
    def from_domain(cls, value: AssignWorkItemResult) -> "AssignWorkItemResponseDto":
        return cls(
            work_item=WorkItemResponseDto.from_domain(value.work_item),
            assignment=WorkItemAssignmentResponseDto.from_domain(value.assignment),
        )


class GateReassignmentResponseDto(CamelModel):
    gate: GateInstanceResponseDto
    assignment: GateAssignmentResponseDto

    @classmethod
    def from_domain(cls, value: GateReassignmentResult) -> "GateReassignmentResponseDto":
        return cls(
            gate=GateInstanceResponseDto.from_domain(value.gate),
            assignment=GateAssignmentResponseDto.from_domain(value.assignment),
        )


class RequirementDetailsResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_items: list[WorkItemResponseDto]
    work_item_assignments: list[WorkItemAssignmentResponseDto]
    current_sdd_baseline: SddBaselineResponseDto | None
    current_gate: GateInstanceResponseDto | None
    current_gate_assignment: GateAssignmentResponseDto | None
    current_decision: DecisionResponseDto | None

    @classmethod
    def from_domain(cls, value: RequirementDetailsDto) -> "RequirementDetailsResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_items=[WorkItemResponseDto.from_domain(item) for item in value.work_items],
            work_item_assignments=[
                WorkItemAssignmentResponseDto.from_domain(item)
                for item in value.work_item_assignments
            ],
            current_sdd_baseline=(
                None
                if value.current_sdd_baseline is None
                else SddBaselineResponseDto.from_domain(value.current_sdd_baseline)
            ),
            current_gate=(
                None
                if value.current_gate is None
                else GateInstanceResponseDto.from_domain(value.current_gate)
            ),
            current_gate_assignment=(
                None
                if value.current_gate_assignment is None
                else GateAssignmentResponseDto.from_domain(value.current_gate_assignment)
            ),
            current_decision=(
                None
                if value.current_decision is None
                else DecisionResponseDto.from_domain(value.current_decision)
            ),
        )
