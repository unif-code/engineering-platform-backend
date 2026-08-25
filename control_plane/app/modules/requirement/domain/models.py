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
