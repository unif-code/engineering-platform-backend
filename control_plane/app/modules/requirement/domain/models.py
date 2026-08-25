from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RequirementType(StrEnum):
    FEAT = "feat"
    FIX = "fix"
    REFACTOR = "refactor"
    CHORE = "chore"


class RequirementState(StrEnum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    READY = "READY"
    CANCELED = "CANCELED"


class RecordState(StrEnum):
    ACTIVE = "ACTIVE"


class AssignmentState(StrEnum):
    UNASSIGNED = "UNASSIGNED"
    ASSIGNED = "ASSIGNED"


class RepositoryState(StrEnum):
    WAITING_REPOSITORY = "WAITING_REPOSITORY"
    BOUND = "BOUND"


class WorkItemState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    CANCELED = "CANCELED"


class ExecutorType(StrEnum):
    HUMAN = "HUMAN"


class GateType(StrEnum):
    REQUIREMENT_BASELINE_CONFIRMATION = "REQUIREMENT_BASELINE_CONFIRMATION"


class GateState(StrEnum):
    OPEN = "OPEN"
    DECIDED = "DECIDED"


class DecisionOutcome(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REJECTED = "REJECTED"


class RequirementDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    workspace_id: str
    type: RequirementType
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    created_by: str
    initial_repository_id: str
    route_snapshot_version: int
    route_snapshot_hash: str
    state: RequirementState
    record_state: RecordState
    requirement_version: int
    required_work_item_set_version: int
    required_work_item_set_hash: str
    current_sdd_baseline_id: str | None
    revision: int
    created_at: datetime
    updated_at: datetime


class WorkItemDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    requirement_id: str
    created_by: str
    human_owner_id: str | None
    executor_type: ExecutorType
    executor_id: str | None
    required_capabilities: tuple[str, ...]
    assignment_state: AssignmentState
    repository_state: RepositoryState
    state: WorkItemState
    repository_id: str
    base_commit_sha: str | None
    task_branch: str | None
    revision: int
    created_at: datetime
    updated_at: datetime


class CreateRequirementResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    work_item: WorkItemDto


class RequirementDetailsDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    work_items: tuple[WorkItemDto, ...]


class RequirementPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[RequirementDto, ...]
    next_cursor: str | None


class SddBaselineDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    requirement_id: str
    requirement_version: int
    artifact_id: str
    artifact_version: str
    artifact_hash: str
    route_snapshot_version: int
    route_snapshot_hash: str
    created_by: str
    created_at: datetime


class RegisterSddBaselineResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    baseline: SddBaselineDto


class GateInstanceDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    gate_type: GateType
    requirement_id: str
    requirement_version: int
    sdd_baseline_id: str
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


class GateAssignmentDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    gate_instance_id: str
    default_reviewer_id: str
    current_reviewer_id: str
    revision: int
    assigned_at: datetime
    superseded_at: datetime | None


class DecisionDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    gate_instance_id: str
    gate_assignment_id: str
    reviewer_id: str
    outcome: DecisionOutcome
    reason: str
    subject_revision: int
    decided_at: datetime


class BaselineConfirmationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    gate: GateInstanceDto
    assignment: GateAssignmentDto


class BaselineDecisionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    gate: GateInstanceDto
    decision: DecisionDto
