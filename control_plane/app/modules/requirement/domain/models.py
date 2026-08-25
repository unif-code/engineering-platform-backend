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
