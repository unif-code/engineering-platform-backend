"""Agent-owned bridge to shared authenticated command execution, in the caller UoW."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.domain import (
    AgentIdempotencyRecord,
    IdempotencyCompletion,
    IdempotencyState,
)
from control_plane.app.modules.agent.ports import AgentRepository
from control_plane.app.shared.idempotency import (
    IdempotencyConflict as SharedIdempotencyConflict,
)
from control_plane.app.shared.idempotency import (
    IdempotencyReplayUnavailable,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from control_plane.app.shared.security import SecretMaterialUnavailable


class IdempotencyConflict(ValueError):
    pass


class IdempotencyInProgress(ValueError):
    pass


class AgentReplayUnavailable(RuntimeError):
    """No protected effect may run when a stored result cannot be authenticated."""


class _SharedRepository:
    def __init__(self, repository: AgentRepository) -> None:
        self.repository = repository

    def claim_idempotency(self, **values: Any) -> bool:
        return self.repository.reserve_idempotency(
            AgentIdempotencyRecord(
                id=values["id"],
                actor=values["actor"],
                operation=values["operation"],
                key=values["idempotency_key"],
                request_fingerprint=values["request_fingerprint"],
                state=IdempotencyState.IN_PROGRESS,
                http_status=None,
                result_metadata=None,
                sealed_response=None,
                created_at=values["now"],
                updated_at=values["now"],
                completed_at=None,
            )
        ).created

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        record = self.repository.idempotency_by_scope(
            actor, operation, idempotency_key, for_update=for_update
        )
        if record is None:
            return None
        # Old plaintext rows are evidence, never candidates for execution or fallback replay.
        if record.state is IdempotencyState.COMPLETED and record.result_metadata != {
            "kind": "http-response",
            "schemaVersion": 1,
        }:
            raise AgentReplayUnavailable("Agent response replay is unavailable")
        values = record.model_dump(mode="python")
        values["idempotency_key"] = values.pop("key")
        return values

    def complete_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool:
        return (
            self.repository.complete_idempotency(
                record_id,
                completion=IdempotencyCompletion(
                    http_status=http_status,
                    result_metadata=result_metadata,
                    sealed_response=sealed_response,
                    now=now,
                ),
            )
            is not None
        )


def execute_agent_command[T: BaseModel](
    *,
    repository: AgentRepository,
    dependencies: AgentDependencies,
    actor: str,
    operation: str,
    key: str,
    path: str,
    body: Mapping[str, object],
    result_type: type[T],
    command: Callable[[], T],
) -> T:
    try:
        material = dependencies.secret_manager.load()
        fingerprint = canonical_request_fingerprint(
            operation=operation,
            method="POST",
            path=path,
            body=body,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
        # Keep the complete Facade result in the authenticated response body. HTTP
        # projection uses this original snapshot (including its original revision/ETag).
        execution = execute_idempotent(
            _SharedRepository(repository),
            actor=actor,
            operation=operation,
            key=key,
            fingerprint=fingerprint,
            command=lambda: IdempotentResponse(
                status_code=202, body=command().model_dump(mode="json")
            ),
            now=dependencies.clock,
            new_id=dependencies.new_id,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except (SecretMaterialUnavailable, IdempotencyReplayUnavailable):
        raise AgentReplayUnavailable("Agent response replay is unavailable") from None
    except SharedIdempotencyConflict as error:
        if "in progress" in str(error):
            raise IdempotencyInProgress("Idempotency-Key reservation is in progress") from None
        raise IdempotencyConflict("Idempotency-Key is bound to a different request") from None
    try:
        return result_type.model_validate(execution.response.body)
    except ValidationError:
        raise AgentReplayUnavailable("Agent response replay is unavailable") from None
