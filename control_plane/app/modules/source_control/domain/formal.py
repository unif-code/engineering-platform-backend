from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from control_plane.app.modules.source_control.domain.integration import (
    MergeRequestBindingDto,
    MergeRequestObservationDto,
)
from control_plane.app.modules.source_control.domain.models import SourceControlEffectDto
from control_plane.app.modules.source_control.domain.transitions import SourceControlError


class FormalDeliveryConflict(SourceControlError):
    pass


class FormalDeliveryRequestKind(StrEnum):
    CREATE_MR = "CREATE_MR"
    MERGE_MR = "MERGE_MR"


class FormalDeliveryRequestEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    topic: Literal[
        "requirement.formal-merge-request.requested",
        "requirement.formal-merge.requested",
    ]
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

    @model_validator(mode="after")
    def validate_shape(self) -> "FormalDeliveryRequestEnvelope":
        create = (
            self.kind is FormalDeliveryRequestKind.CREATE_MR
            and self.topic == "requirement.formal-merge-request.requested"
            and self.formal_review_decision_id is None
        )
        merge = (
            self.kind is FormalDeliveryRequestKind.MERGE_MR
            and self.topic == "requirement.formal-merge.requested"
            and self.formal_merge_request_binding_id is not None
            and self.formal_review_decision_id is not None
        )
        if not (create or merge):
            raise ValueError("formal delivery request shape is invalid")
        return self


class FormalReviewRoutingSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    default_reviewer_id: str
    policy_code: str
    policy_version: int = Field(ge=1)
    policy_snapshot_hash: str
    resolution_snapshot: dict[str, object]


class FormalReviewAssignmentDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    binding_id: str
    acceptance_decision_id: str
    requirement_id: str
    work_item_id: str
    subject_head_sha: str
    default_reviewer_id: str
    current_reviewer_id: str
    policy_code: str
    policy_version: int = Field(ge=1)
    policy_snapshot_hash: str
    resolution_snapshot: dict[str, object]
    revision: int = Field(ge=1)
    assigned_at: AwareDatetime
    superseded_at: AwareDatetime | None


class ProcessFormalDeliveryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    effect: SourceControlEffectDto | None
    binding: MergeRequestBindingDto | None
    observation: MergeRequestObservationDto | None
    assignment: FormalReviewAssignmentDto | None = None
    blocked_reason: str | None = None


class RelayFormalDeliveryRequestsResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    claimed: int = Field(ge=0)
    accepted: int = Field(ge=0)
    released: int = Field(ge=0)
