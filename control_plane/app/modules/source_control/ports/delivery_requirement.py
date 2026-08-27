from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.source_control.domain import DeliveryRequestEnvelope
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports._callback import _CorrelatedCallbackResult


class RequirementDeliveryContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement_id: str
    requirement_revision: int
    requirement_state: str
    workspace_id: str
    work_item_id: str
    work_item_revision: int
    work_item_state: str
    repository_id: str
    repository_state: str
    human_owner_id: str | None
    required_capabilities: tuple[str, ...]
    base_commit_sha: str | None
    task_branch: str | None
    integration_delivery_state: str
    integration_merge_request_binding_id: str | None
    request_actor_id: str


class IntegrationMrReadyResult(_CorrelatedCallbackResult):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    binding_id: str
    expected_revision: int
    idempotency_key: str


class IntegrationDeliveryBlockedResult(_CorrelatedCallbackResult):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    binding_id: str | None
    reason_code: SourceControlReason
    expected_revision: int
    idempotency_key: str


class IntegrationReconciliationPendingResult(_CorrelatedCallbackResult):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    binding_id: str | None
    expected_revision: int
    idempotency_key: str


class IntegrationMergedResult(_CorrelatedCallbackResult):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    binding_id: str
    expected_revision: int
    idempotency_key: str


class ExternalMergeDriftResult(_CorrelatedCallbackResult):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    binding_id: str
    expected_revision: int
    idempotency_key: str


class RelayIntegrationDeliveryRequestsResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    claimed: int = Field(ge=0)
    accepted: int = Field(ge=0)
    released: int = Field(ge=0)


class RequirementDeliveryPort(Protocol):
    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[DeliveryRequestEnvelope, ...]: ...

    def acknowledge_request(self, message_id: str) -> None: ...

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None: ...

    def delivery_context(self, work_item_id: str) -> RequirementDeliveryContext: ...

    def record_mr_ready(self, result: IntegrationMrReadyResult) -> None: ...

    def record_blocked(self, result: IntegrationDeliveryBlockedResult) -> None: ...

    def record_pending(self, result: IntegrationReconciliationPendingResult) -> None: ...

    def record_merged(self, result: IntegrationMergedResult) -> None: ...

    def record_external_merge_drift(self, result: ExternalMergeDriftResult) -> None: ...
