from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Engine

import control_plane.app.modules.requirement as requirement
from control_plane.app.modules.source_control.domain import (
    FormalDeliveryRequestEnvelope,
    FormalDeliveryRequestKind,
    RequirementCallbackUnavailable,
)
from control_plane.app.modules.source_control.ports import (
    FormalDeliveryAdmission,
    FormalDeliveryBlockedCallback,
    FormalMergedCallback,
    FormalMrReadyCallback,
)

_SYSTEM_ACTOR = SimpleNamespace(account_id="SYSTEM:SOURCE_CONTROL")


@dataclass(frozen=True, slots=True)
class RequirementFacadeFormalDeliveryAdapter:
    engine: Engine
    dependencies: Any

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[FormalDeliveryRequestEnvelope, ...]:
        try:
            with self.engine.begin() as db:
                messages = requirement.claim_formal_delivery_requests(
                    db,
                    limit=limit,
                    available_before=self.dependencies.clock.now(),
                    lease_until=lease_until,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Formal Delivery claim unavailable"
            ) from None
        return tuple(
            FormalDeliveryRequestEnvelope(
                message_id=message.message_id,
                topic=(
                    "requirement.formal-merge-request.requested"
                    if message.kind.value == "CREATE_MR"
                    else "requirement.formal-merge.requested"
                ),
                payload_hash=message.payload_hash,
                requirement_id=message.requirement_id,
                requirement_revision=message.requirement_revision,
                work_item_id=message.work_item_id,
                work_item_revision=message.work_item_revision,
                repository_id=message.repository_id,
                actor_id=message.actor_id,
                acceptance_decision_id=message.acceptance_decision_id,
                formal_merge_request_binding_id=(message.formal_merge_request_binding_id),
                formal_review_decision_id=message.formal_review_decision_id,
                requested_head_sha=message.requested_head_sha,
                kind=FormalDeliveryRequestKind(message.kind.value),
                attempts=message.attempts,
            )
            for message in messages
        )

    def acknowledge_request(self, message_id: str) -> None:
        try:
            with self.engine.begin() as db:
                requirement.acknowledge_formal_delivery_request(
                    db,
                    message_id=message_id,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Formal Delivery acknowledgement unavailable"
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
                requirement.release_formal_delivery_request(
                    db,
                    message_id=message_id,
                    error_code=error_code,
                    available_at=retry_at,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Formal Delivery release unavailable"
            ) from None

    def delivery_admission(self, work_item_id: str) -> FormalDeliveryAdmission:
        try:
            with self.engine.connect() as db:
                admission = requirement.get_formal_delivery_admission(
                    db,
                    work_item_id=work_item_id,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Formal Delivery admission unavailable"
            ) from None
        return FormalDeliveryAdmission.model_validate(admission.model_dump(mode="json"))

    def record_mr_ready(self, callback: FormalMrReadyCallback) -> None:
        try:
            with self.engine.begin() as db:
                requirement.record_formal_mr_ready(
                    db,
                    work_item_id=callback.work_item_id,
                    binding_id=callback.binding_id,
                    head_sha=callback.head_sha,
                    expected_revision=callback.expected_revision,
                    assignment=requirement.DeliveryGatePolicySnapshot(
                        version=callback.assignment.policy_version,
                        default_reviewer_id=callback.assignment.default_reviewer_id,
                        policy_code=callback.assignment.policy_code,
                        snapshot_hash=callback.assignment.policy_snapshot_hash,
                        resolution_snapshot=callback.assignment.resolution_snapshot,
                    ),
                    actor=_SYSTEM_ACTOR,
                    idempotency_key=callback.idempotency_key,
                    correlation_id=callback.correlation_id,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Formal MR ready callback unavailable"
            ) from None

    def record_blocked(self, callback: FormalDeliveryBlockedCallback) -> None:
        try:
            reason_code = requirement.FormalDeliveryBlockedReason(callback.reason_code.value)
            with self.engine.begin() as db:
                requirement.record_formal_delivery_blocked(
                    db,
                    work_item_id=callback.work_item_id,
                    binding_id=callback.binding_id,
                    reason_code=reason_code,
                    expected_revision=callback.expected_revision,
                    actor=_SYSTEM_ACTOR,
                    idempotency_key=callback.idempotency_key,
                    correlation_id=callback.correlation_id,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Formal blocked callback unavailable"
            ) from None

    def record_merged(self, callback: FormalMergedCallback) -> None:
        try:
            with self.engine.begin() as db:
                requirement.record_formal_merged(
                    db,
                    work_item_id=callback.work_item_id,
                    binding_id=callback.binding_id,
                    head_sha=callback.head_sha,
                    merge_commit_sha=callback.merge_commit_sha,
                    expected_revision=callback.expected_revision,
                    actor=_SYSTEM_ACTOR,
                    idempotency_key=callback.idempotency_key,
                    correlation_id=callback.correlation_id,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Formal merge callback unavailable"
            ) from None
