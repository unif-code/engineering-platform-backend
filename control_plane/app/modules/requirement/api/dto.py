from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field

from control_plane.app.modules.requirement.domain import (
    AssignmentState,
    BaselineConfirmationResult,
    BaselineDecisionResult,
    CreateRequirementResult,
    DecisionDto,
    DecisionOutcome,
    ExecutorType,
    GateAssignmentDto,
    GateInstanceDto,
    GateState,
    GateType,
    RecordState,
    RegisterSddBaselineResult,
    RepositoryState,
    RequirementDetailsDto,
    RequirementDto,
    RequirementPage,
    RequirementState,
    RequirementType,
    SddBaselineDto,
    WorkItemDto,
    WorkItemState,
)
from control_plane.app.shared.api.camel import CamelModel


class StrictCamelModel(CamelModel):
    model_config = ConfigDict(extra="forbid")


class CreateRequirementRequestDto(StrictCamelModel):
    workspace_id: UUID
    type: RequirementType
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10000)
    acceptance_criteria: list[str] = Field(min_length=1)
    initial_repository_id: str = Field(min_length=1)


class RegisterSddBaselineRequestDto(StrictCamelModel):
    artifact_id: str = Field(min_length=1)
    artifact_version: str = Field(min_length=1)


class SubmitBaselineConfirmationRequestDto(StrictCamelModel):
    sdd_baseline_id: UUID


class DecideBaselineRequestDto(StrictCamelModel):
    gate_id: UUID
    outcome: DecisionOutcome
    reason: str = Field(min_length=1, max_length=2000)


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


class RequirementDetailsResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_items: list[WorkItemResponseDto]

    @classmethod
    def from_domain(cls, value: RequirementDetailsDto) -> "RequirementDetailsResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_items=[WorkItemResponseDto.from_domain(item) for item in value.work_items],
        )


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
    policy_version: int
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
