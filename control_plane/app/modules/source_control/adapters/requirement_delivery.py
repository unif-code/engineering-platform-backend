from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Engine

import control_plane.app.modules.requirement as requirement
from control_plane.app.modules.source_control.adapters.requirement import (
    SourceControlSystemActor,
)
from control_plane.app.modules.source_control.domain import (
    DeliveryRequestEnvelope,
    DeliveryRequestKind,
    RequirementCallbackUnavailable,
)
from control_plane.app.modules.source_control.ports.delivery_requirement import (
    ExternalMergeDriftResult,
    IntegrationDeliveryBlockedResult,
    IntegrationMergedResult,
    IntegrationMrReadyResult,
    IntegrationReconciliationPendingResult,
    RequirementDeliveryContext,
)

type DeliveryTopic = Literal[
    "requirement.integration-merge-request.requested",
    "requirement.integration-merge.requested",
]

_DELIVERY_TOPICS: dict[DeliveryRequestKind, DeliveryTopic] = {
    DeliveryRequestKind.CREATE_MR: "requirement.integration-merge-request.requested",
    DeliveryRequestKind.MERGE_MR: "requirement.integration-merge.requested",
}


@dataclass(frozen=True, slots=True)
class RequirementFacadeDeliveryAdapter:
    engine: Engine
    dependencies: Any
    actor: object = SourceControlSystemActor()

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[DeliveryRequestEnvelope, ...]:
        try:
            available_before = self.dependencies.clock.now()
            with self.engine.begin() as db:
                messages = requirement.claim_integration_delivery_requests(
                    db,
                    limit=limit,
                    available_before=available_before,
                    lease_until=lease_until,
                    dependencies=self.dependencies,
                )
            return tuple(
                DeliveryRequestEnvelope(
                    message_id=message.message_id,
                    topic=_DELIVERY_TOPICS[DeliveryRequestKind(message.kind.value)],
                    payload_hash=message.payload_hash,
                    requirement_id=message.requirement_id,
                    requirement_revision=message.requirement_revision,
                    work_item_id=message.work_item_id,
                    work_item_revision=message.work_item_revision,
                    repository_id=message.repository_id,
                    actor_id=message.actor_id,
                    kind=DeliveryRequestKind(message.kind.value),
                    integration_merge_request_binding_id=(
                        message.integration_merge_request_binding_id
                    ),
                    attempts=message.attempts,
                )
                for message in messages
            )
        except Exception:
            raise RequirementCallbackUnavailable("Requirement claim unavailable") from None

    def acknowledge_request(self, message_id: str) -> None:
        try:
            with self.engine.begin() as db:
                requirement.acknowledge_integration_delivery_request(
                    db,
                    message_id=message_id,
                    consumer="SOURCE_CONTROL",
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement acknowledgement unavailable"
            ) from None

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        try:
            with self.engine.begin() as db:
                requirement.release_integration_delivery_request(
                    db,
                    message_id=message_id,
                    error_code=error_code,
                    available_at=retry_at,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable("Requirement release unavailable") from None

    def delivery_context(self, work_item_id: str) -> RequirementDeliveryContext:
        try:
            with self.engine.begin() as db:
                context = requirement.get_integration_delivery_context(
                    db,
                    work_item_id=work_item_id,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable("Requirement context unavailable") from None
        return RequirementDeliveryContext(
            requirement_id=context.requirement_id,
            requirement_state=context.requirement_state.value,
            workspace_id=context.workspace_id,
            work_item_id=context.work_item_id,
            work_item_revision=context.work_item_revision,
            work_item_state=context.work_item_state.value,
            repository_id=context.repository_id,
            repository_state=context.repository_state.value,
            human_owner_id=context.human_owner_id,
            required_capabilities=context.required_capabilities,
            base_commit_sha=context.base_commit_sha,
            task_branch=context.task_branch,
            integration_delivery_state=context.integration_delivery_state.value,
            integration_merge_request_binding_id=(context.integration_merge_request_binding_id),
            request_actor_id=context.request_actor_id,
        )

    def record_mr_ready(self, result: IntegrationMrReadyResult) -> None:
        try:
            with self.engine.begin() as db:
                requirement.record_integration_mr_ready(
                    db,
                    work_item_id=result.work_item_id,
                    binding_id=result.binding_id,
                    expected_revision=result.expected_revision,
                    actor=self.actor,
                    idempotency_key=result.idempotency_key,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement MR-ready callback unavailable"
            ) from None

    def record_blocked(self, result: IntegrationDeliveryBlockedResult) -> None:
        try:
            reason_code = requirement.IntegrationDeliveryBlockedReason(result.reason_code)
            with self.engine.begin() as db:
                requirement.record_integration_delivery_blocked(
                    db,
                    work_item_id=result.work_item_id,
                    binding_id=result.binding_id,
                    reason_code=reason_code,
                    expected_revision=result.expected_revision,
                    actor=self.actor,
                    idempotency_key=result.idempotency_key,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement blocked callback unavailable"
            ) from None

    def record_pending(self, result: IntegrationReconciliationPendingResult) -> None:
        try:
            with self.engine.begin() as db:
                requirement.record_integration_reconciliation_pending(
                    db,
                    work_item_id=result.work_item_id,
                    binding_id=result.binding_id,
                    expected_revision=result.expected_revision,
                    actor=self.actor,
                    idempotency_key=result.idempotency_key,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement reconciliation callback unavailable"
            ) from None

    def record_merged(self, result: IntegrationMergedResult) -> None:
        try:
            with self.engine.begin() as db:
                requirement.record_integration_merged(
                    db,
                    work_item_id=result.work_item_id,
                    binding_id=result.binding_id,
                    expected_revision=result.expected_revision,
                    actor=self.actor,
                    idempotency_key=result.idempotency_key,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement merged callback unavailable"
            ) from None

    def record_external_merge_drift(self, result: ExternalMergeDriftResult) -> None:
        try:
            with self.engine.begin() as db:
                requirement.record_external_merge_drift(
                    db,
                    work_item_id=result.work_item_id,
                    binding_id=result.binding_id,
                    expected_revision=result.expected_revision,
                    actor=self.actor,
                    idempotency_key=result.idempotency_key,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement external-drift callback unavailable"
            ) from None
