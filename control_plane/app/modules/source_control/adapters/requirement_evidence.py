from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine

import control_plane.app.modules.requirement as requirement
from control_plane.app.modules.source_control.domain import (
    ArtifactReference,
    ExternalValidationRequestEnvelope,
    IntegrationBaselineRequestEnvelope,
    RequirementCallbackUnavailable,
)


@dataclass(frozen=True, slots=True)
class RequirementFacadeEvidenceAdapter:
    engine: Engine
    dependencies: Any

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[
        ExternalValidationRequestEnvelope | IntegrationBaselineRequestEnvelope,
        ...,
    ]:
        try:
            with self.engine.begin() as db:
                messages = requirement.claim_evidence_requests(
                    db,
                    limit=limit,
                    available_before=self.dependencies.clock.now(),
                    lease_until=lease_until,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable("Requirement Evidence claim unavailable") from None
        converted: list[ExternalValidationRequestEnvelope | IntegrationBaselineRequestEnvelope] = []
        for message in messages:
            if isinstance(message, requirement.ExternalValidationRequestMessage):
                converted.append(
                    ExternalValidationRequestEnvelope(
                        message_id=message.message_id,
                        payload_hash=message.payload_hash,
                        requirement_id=message.requirement_id,
                        requirement_version=message.requirement_version,
                        work_item_id=message.work_item_id,
                        work_item_revision=message.work_item_revision,
                        repository_id=message.repository_id,
                        integration_merge_request_binding_id=(
                            message.integration_merge_request_binding_id
                        ),
                        target_commit_sha=message.target_commit_sha,
                        integration_merge_commit_sha=(message.integration_merge_commit_sha),
                        reference=message.reference,
                        notes=message.notes,
                        artifact_references=tuple(
                            ArtifactReference(
                                artifact_id=item.artifact_id,
                                artifact_version=item.artifact_version,
                                artifact_hash=item.artifact_hash,
                            )
                            for item in message.artifact_references
                        ),
                        submitted_by=message.submitted_by,
                        submitted_at=message.submitted_at,
                        attempts=message.attempts,
                    )
                )
            else:
                converted.append(
                    IntegrationBaselineRequestEnvelope(
                        message_id=message.message_id,
                        payload_hash=message.payload_hash,
                        delivery_snapshot_id=message.delivery_snapshot_id,
                        delivery_snapshot_hash=message.delivery_snapshot_hash,
                        requirement_id=message.requirement_id,
                        requirement_version=message.requirement_version,
                        required_work_item_set_version=(message.required_work_item_set_version),
                        required_work_item_set_hash=message.required_work_item_set_hash,
                        work_item_ids=message.work_item_ids,
                        requested_by=message.requested_by,
                        attempts=message.attempts,
                    )
                )
        return tuple(converted)

    def acknowledge_request(self, message_id: str) -> None:
        try:
            with self.engine.begin() as db:
                requirement.acknowledge_evidence_request(
                    db,
                    message_id=message_id,
                    consumer="SOURCE_CONTROL",
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Evidence acknowledgement unavailable"
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
                requirement.release_evidence_request(
                    db,
                    message_id=message_id,
                    error_code=error_code,
                    available_at=retry_at,
                    dependencies=self.dependencies,
                )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Evidence release unavailable"
            ) from None
