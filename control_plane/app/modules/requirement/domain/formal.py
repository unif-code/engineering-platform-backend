from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.requirement.domain.acceptance import (
    DeliveryGateAssignmentDto,
    DeliveryGateDto,
)
from control_plane.app.modules.requirement.domain.models import RequirementDto, WorkItemDto
from control_plane.app.modules.requirement.domain.transitions import RequirementError


class FormalDeliveryConflict(RequirementError):
    pass


class FormalReviewStale(FormalDeliveryConflict):
    """The Formal Review subject is no longer current."""


class FormalDeliveryBlocked(FormalDeliveryConflict):
    """Formal Delivery cannot advance from the current accepted subject."""


class FormalDeliveryRequestKind(StrEnum):
    CREATE_MR = "CREATE_MR"
    MERGE_MR = "MERGE_MR"


class FormalDeliveryBlockedReason(StrEnum):
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


class FormalDeliveryCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    work_item: WorkItemDto
    outbox_topic: str


class FormalDeliveryBlockedResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    work_item: WorkItemDto
    reason_code: FormalDeliveryBlockedReason


class FormalDeliveryRequestMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    payload_hash: str
    requirement_id: str
    requirement_revision: int = Field(ge=1)
    work_item_id: str
    work_item_revision: int = Field(ge=1)
    repository_id: str
    actor_id: str
    acceptance_decision_id: str
    formal_merge_request_binding_id: str | None
    formal_review_decision_id: str | None
    requested_head_sha: str
    kind: FormalDeliveryRequestKind
    attempts: int = Field(ge=1)


class FormalDeliveryAdmission(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement_id: str
    requirement_revision: int = Field(ge=1)
    requirement_version: int = Field(ge=1)
    workspace_id: str
    work_item_id: str
    work_item_revision: int = Field(ge=1)
    repository_id: str
    task_branch: str
    requested_head_sha: str
    human_owner_id: str
    acceptance_decision_id: str
    formal_merge_request_binding_id: str | None
    formal_review_decision_id: str | None


class FormalMrReadyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    work_item: WorkItemDto
    gate: DeliveryGateDto
    assignment: DeliveryGateAssignmentDto


class FormalMergedResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: RequirementDto
    work_item: WorkItemDto
