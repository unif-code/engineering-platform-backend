from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine

import control_plane.app.modules.requirement as requirement
from control_plane.app.modules.source_control.domain import (
    BindingRequestEnvelope,
    RequirementCallbackUnavailable,
)
from control_plane.app.modules.source_control.ports import (
    BindingBlockedResult,
    BindingReadyResult,
    ClockPort,
    RequirementBindingContext,
)


@dataclass(frozen=True, slots=True)
class SourceControlSystemActor:
    account_id: str = "source-control-worker"


@dataclass(frozen=True, slots=True)
class RequirementFacadeBindingAdapter:
    engine: Engine
    dependencies: Any
    clock: ClockPort
    actor: object = SourceControlSystemActor()

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[BindingRequestEnvelope, ...]:
        try:
            with self.engine.begin() as db:
                messages = requirement.claim_repository_binding_requests(
                    db,
                    limit=limit,
                    available_before=self.clock.now(),
                    lease_until=lease_until,
                    dependencies=self.dependencies,
                )
        except Exception as error:
            raise RequirementCallbackUnavailable("Requirement claim unavailable") from error
        return tuple(
            BindingRequestEnvelope(
                message_id=message.message_id,
                topic="requirement.repository-binding.requested",
                requirement_id=message.requirement_id,
                requirement_version=message.requirement_version,
                work_item_id=message.work_item_id,
                repository_id=message.repository_id,
                attempts=message.attempts,
            )
            for message in messages
        )

    def acknowledge_request(self, message_id: str) -> None:
        try:
            with self.engine.begin() as db:
                requirement.acknowledge_repository_binding_request(
                    db,
                    message_id=message_id,
                    consumer="SOURCE_CONTROL",
                    dependencies=self.dependencies,
                )
        except Exception as error:
            raise RequirementCallbackUnavailable(
                "Requirement acknowledgement unavailable"
            ) from error

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        try:
            with self.engine.begin() as db:
                requirement.release_repository_binding_request(
                    db,
                    message_id=message_id,
                    error_code=error_code,
                    available_at=retry_at,
                    dependencies=self.dependencies,
                )
        except Exception as error:
            raise RequirementCallbackUnavailable("Requirement release unavailable") from error

    def binding_context(self, work_item_id: str) -> RequirementBindingContext:
        try:
            with self.engine.begin() as db:
                context = requirement.get_repository_binding_context(
                    db,
                    work_item_id=work_item_id,
                    dependencies=self.dependencies,
                )
        except Exception as error:
            raise RequirementCallbackUnavailable("Requirement context unavailable") from error
        return RequirementBindingContext(
            requirement_id=context.requirement_id,
            requirement_type=context.requirement_type.value,
            requirement_title=context.requirement_title,
            workspace_id=context.workspace_id,
            work_item_id=context.work_item_id,
            work_item_revision=context.work_item_revision,
            repository_id=context.repository_id,
            assignment_state=context.assignment_state.value,
            human_owner_id=context.human_owner_id,
            required_capabilities=context.required_capabilities,
        )

    def record_ready(self, result: BindingReadyResult) -> None:
        try:
            with self.engine.begin() as db:
                requirement.record_repository_binding(
                    db,
                    work_item_id=result.work_item_id,
                    repository_id=result.repository_id,
                    base_commit_sha=result.base_commit_sha,
                    task_branch=result.task_branch,
                    expected_revision=result.expected_revision,
                    actor=self.actor,
                    idempotency_key=result.idempotency_key,
                    dependencies=self.dependencies,
                )
        except Exception as error:
            raise RequirementCallbackUnavailable(
                "Requirement ready callback unavailable"
            ) from error

    def record_blocked(self, result: BindingBlockedResult) -> None:
        try:
            reason_code = requirement.RepositoryBindingBlockedReason(result.reason_code)
            with self.engine.begin() as db:
                requirement.record_repository_binding_blocked(
                    db,
                    work_item_id=result.work_item_id,
                    repository_id=result.repository_id,
                    reason_code=reason_code,
                    expected_revision=result.expected_revision,
                    actor=self.actor,
                    idempotency_key=result.idempotency_key,
                    dependencies=self.dependencies,
                )
        except Exception as error:
            raise RequirementCallbackUnavailable(
                "Requirement blocked callback unavailable"
            ) from error
