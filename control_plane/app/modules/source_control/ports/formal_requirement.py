from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.source_control.domain import (
    FormalDeliveryRequestEnvelope,
    FormalReviewRoutingSnapshot,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason


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


class FormalMrReadyCallback(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    binding_id: str
    head_sha: str
    expected_revision: int = Field(ge=1)
    assignment: FormalReviewRoutingSnapshot
    correlation_id: str
    idempotency_key: str


class FormalMergedCallback(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    binding_id: str
    head_sha: str
    merge_commit_sha: str
    expected_revision: int = Field(ge=1)
    correlation_id: str
    idempotency_key: str


class FormalDeliveryBlockedCallback(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    binding_id: str | None
    reason_code: SourceControlReason
    expected_revision: int = Field(ge=1)
    correlation_id: str
    idempotency_key: str


class RequirementFormalDeliveryPort(Protocol):
    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[FormalDeliveryRequestEnvelope, ...]: ...

    def acknowledge_request(self, message_id: str) -> None: ...

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None: ...

    def delivery_admission(self, work_item_id: str) -> FormalDeliveryAdmission: ...

    def record_mr_ready(self, callback: FormalMrReadyCallback) -> None: ...

    def record_blocked(self, callback: FormalDeliveryBlockedCallback) -> None: ...

    def record_merged(self, callback: FormalMergedCallback) -> None: ...


class FormalReviewRoutingPort(Protocol):
    def resolve(
        self,
        *,
        workspace_id: str,
        repository_id: str,
        work_item_id: str,
        human_owner_id: str,
    ) -> FormalReviewRoutingSnapshot: ...
