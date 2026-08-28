from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    CANCELED = "CANCELED"


class RecordState(StrEnum):
    ACTIVE = "ACTIVE"


class AssignmentState(StrEnum):
    UNASSIGNED = "UNASSIGNED"
    ASSIGNED = "ASSIGNED"


class RepositoryState(StrEnum):
    WAITING_REPOSITORY = "WAITING_REPOSITORY"
    BLOCKED = "BLOCKED"
    BOUND = "BOUND"


class RepositoryBindingBlockedReason(StrEnum):
    CONNECTOR_UNAVAILABLE = "CONNECTOR_UNAVAILABLE"
    REPOSITORY_NOT_FOUND = "REPOSITORY_NOT_FOUND"
    ACCESS_DENIED = "ACCESS_DENIED"
    POLICY_DENIED = "POLICY_DENIED"
    BINDING_CONFLICT = "BINDING_CONFLICT"
    OWNER_UNASSIGNED = "OWNER_UNASSIGNED"
    OWNER_INELIGIBLE = "OWNER_INELIGIBLE"
    REPOSITORY_NOT_AUTHORIZED = "REPOSITORY_NOT_AUTHORIZED"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"


class WorkItemState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    CANCELED = "CANCELED"


class IntegrationDeliveryState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IMPLEMENTING = "IMPLEMENTING"
    MR_PENDING = "MR_PENDING"
    MR_OPEN = "MR_OPEN"
    MERGE_PENDING = "MERGE_PENDING"
    INTEGRATED = "INTEGRATED"
    BLOCKED = "BLOCKED"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"


class IntegrationDeliveryRequestKind(StrEnum):
    CREATE_MR = "CREATE_MR"
    MERGE_MR = "MERGE_MR"


class IntegrationDeliveryBlockedReason(StrEnum):
    OWNER_MISMATCH = "OWNER_MISMATCH"
    OWNER_INELIGIBLE = "OWNER_INELIGIBLE"
    MERGE_ACTOR_INELIGIBLE = "MERGE_ACTOR_INELIGIBLE"
    REPOSITORY_NOT_AUTHORIZED = "REPOSITORY_NOT_AUTHORIZED"
    BRANCH_BINDING_MISSING = "BRANCH_BINDING_MISSING"
    TARGET_BRANCH_NOT_FOUND = "TARGET_BRANCH_NOT_FOUND"
    TARGET_BRANCH_NOT_PROTECTED = "TARGET_BRANCH_NOT_PROTECTED"
    NO_DELIVERY_COMMIT = "NO_DELIVERY_COMMIT"
    HEAD_SHA_CHANGED = "HEAD_SHA_CHANGED"
    MR_CONFLICT = "MR_CONFLICT"
    MR_CLOSED = "MR_CLOSED"
    MR_CHECKS_BLOCKED = "MR_CHECKS_BLOCKED"
    MERGE_CONFLICT = "MERGE_CONFLICT"
    PROJECT_PROFILE_UNSUPPORTED = "PROJECT_PROFILE_UNSUPPORTED"
    SOURCE_BRANCH_MISSING_AFTER_INTEGRATION = "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION"
    EXTERNAL_MERGE_DRIFT = "EXTERNAL_MERGE_DRIFT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"


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
    route_snapshot: dict[str, object] = Field(default_factory=dict)
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
    repository_blocked_reason_code: RepositoryBindingBlockedReason | None
    repository_blocked_at: datetime | None
    integration_delivery_state: IntegrationDeliveryState = IntegrationDeliveryState.NOT_STARTED
    integration_merge_request_binding_id: str | None = None
    integration_blocked_reason_code: IntegrationDeliveryBlockedReason | None = None
    integration_updated_at: datetime | None = None
    revision: int
    created_at: datetime
    updated_at: datetime


class WorkItemAssignmentDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    work_item_id: str
    assignee_id: str
    assigned_by: str
    reason: str
    revision: int
    assigned_at: datetime
    superseded_at: datetime | None


class SddArtifactVersionDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str
    version: int
    requirement_id: str
    sha256: str
    state: str
    media_type: str
    trust: str
    content: str
    created_by: str
    created_at: datetime


class RepositoryBindingRequestMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    requirement_id: str
    requirement_version: int
    work_item_id: str
    repository_id: str
    attempts: int


class RepositoryBindingContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement_id: str
    requirement_type: RequirementType
    requirement_title: str
    workspace_id: str
    work_item_id: str
    work_item_revision: int
    repository_id: str
    assignment_state: AssignmentState
    human_owner_id: str | None
    required_capabilities: tuple[str, ...]


class IntegrationDeliveryRequestMessage(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    message_id: str
    payload_hash: str
    requirement_id: str
    requirement_revision: int = Field(ge=1)
    work_item_id: str
    work_item_revision: int = Field(ge=1)
    repository_id: str
    actor_id: str
    kind: IntegrationDeliveryRequestKind
    integration_merge_request_binding_id: str | None
    attempts: int


class IntegrationDeliveryContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement_id: str
    requirement_revision: int
    requirement_state: RequirementState
    workspace_id: str
    work_item_id: str
    work_item_revision: int
    work_item_state: WorkItemState
    repository_id: str
    repository_state: RepositoryState
    human_owner_id: str | None
    required_capabilities: tuple[str, ...]
    base_commit_sha: str | None
    task_branch: str | None
    integration_delivery_state: IntegrationDeliveryState
    integration_merge_request_binding_id: str | None
    request_actor_id: str


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
