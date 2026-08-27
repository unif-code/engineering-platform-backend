from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.source_control.domain import BindingRequestEnvelope
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason


class RequirementBindingContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement_id: str
    requirement_type: str
    requirement_title: str
    workspace_id: str
    work_item_id: str
    work_item_revision: int
    repository_id: str
    assignment_state: str
    human_owner_id: str | None
    required_capabilities: tuple[str, ...]


class BindingEligibility(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: bool
    reason_code: SourceControlReason | None = None


class BindingReadyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    repository_id: str
    base_commit_sha: str
    task_branch: str
    expected_revision: int
    idempotency_key: str


class BindingBlockedResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    repository_id: str
    reason_code: SourceControlReason
    expected_revision: int
    idempotency_key: str


class RequirementBindingPort(Protocol):
    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[BindingRequestEnvelope, ...]: ...

    def acknowledge_request(self, message_id: str) -> None: ...

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None: ...

    def binding_context(self, work_item_id: str) -> RequirementBindingContext: ...

    def record_ready(self, result: BindingReadyResult) -> None: ...

    def record_blocked(self, result: BindingBlockedResult) -> None: ...


class OwnerEligibilityPort(Protocol):
    def evaluate(self, context: RequirementBindingContext) -> BindingEligibility: ...
