import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    BindingRequestEnvelope,
    BindingRequestInboxDto,
    BindingRequestMessageConflict,
    InboxState,
    RelayBindingRequestsResult,
    RepositoryAuthorizationState,
    RepositoryNotFound,
    RepositoryRemoved,
    RequirementCallbackUnavailable,
)
from control_plane.app.modules.source_control.ports import SourceControlRepository


def binding_request_payload_hash(envelope: BindingRequestEnvelope) -> str:
    payload = {
        "messageId": envelope.message_id,
        "topic": envelope.topic,
        "requirementId": envelope.requirement_id,
        "requirementVersion": envelope.requirement_version,
        "workItemId": envelope.work_item_id,
        "repositoryId": envelope.repository_id,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _inbox_dto(row: Any) -> BindingRequestInboxDto:
    return BindingRequestInboxDto(
        message_id=str(row["message_id"]),
        payload_hash=row["payload_hash"],
        requirement_id=str(row["requirement_id"]),
        requirement_version=row["requirement_version"],
        work_item_id=str(row["work_item_id"]),
        repository_id=str(row["repository_id"]),
        state=InboxState(row["state"]),
        attempts=row["attempts"],
        available_at=row["available_at"],
        last_error_code=row["last_error_code"],
        received_at=row["received_at"],
        updated_at=row["updated_at"],
        processed_at=row["processed_at"],
    )


def accept_binding_request(
    repository: SourceControlRepository,
    envelope: BindingRequestEnvelope,
    *,
    now: datetime,
) -> BindingRequestInboxDto:
    payload_hash = binding_request_payload_hash(envelope)
    existing = repository.binding_request(envelope.message_id, for_update=True)
    if existing is not None:
        if existing["payload_hash"] != payload_hash:
            raise BindingRequestMessageConflict(envelope.message_id)
        return _inbox_dto(existing)
    authorized_repository = repository.workspace_repository(envelope.repository_id)
    if authorized_repository is None:
        raise RepositoryNotFound(envelope.repository_id)
    if authorized_repository["status"] != RepositoryAuthorizationState.AUTHORIZED.value:
        raise RepositoryRemoved(envelope.repository_id)
    inserted = repository.accept_binding_request(
        message_id=envelope.message_id,
        payload_hash=payload_hash,
        requirement_id=envelope.requirement_id,
        requirement_version=envelope.requirement_version,
        work_item_id=envelope.work_item_id,
        repository_id=envelope.repository_id,
        now=now,
    )
    if inserted is None:
        existing = repository.binding_request(envelope.message_id, for_update=True)
        if existing is None or existing["payload_hash"] != payload_hash:
            raise BindingRequestMessageConflict(envelope.message_id)
        inserted = existing
    return _inbox_dto(inserted)


def relay_binding_requests(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> RelayBindingRequestsResult:
    requirement = dependencies.requirement
    if requirement is None:
        raise RequirementCallbackUnavailable("Requirement binding port is unavailable")
    now = dependencies.clock.now()
    messages = requirement.claim_requests(
        limit=limit,
        lease_until=now + timedelta(seconds=30),
    )
    accepted = 0
    released = 0
    for envelope in messages:
        try:
            with dependencies.engine.begin() as db:
                accept_binding_request(
                    dependencies.repository_factory(db),
                    envelope,
                    now=now,
                )
        except BindingRequestMessageConflict:
            requirement.release_request(
                envelope.message_id,
                error_code="BINDING_REQUEST_CONFLICT",
                retry_at=now,
            )
            released += 1
            continue
        except (RepositoryNotFound, RepositoryRemoved):
            requirement.release_request(
                envelope.message_id,
                error_code="SOURCE_CONTROL_UNAVAILABLE",
                retry_at=now + timedelta(minutes=5),
            )
            released += 1
            continue
        try:
            requirement.acknowledge_request(envelope.message_id)
        except RequirementCallbackUnavailable:
            raise
        except Exception as error:
            raise RequirementCallbackUnavailable(
                "Requirement acknowledgement unavailable"
            ) from error
        with dependencies.engine.begin() as db:
            dependencies.repository_factory(db).complete_binding_request(
                envelope.message_id,
                now=dependencies.clock.now(),
            )
        accepted += 1
    return RelayBindingRequestsResult(
        claimed=len(messages),
        accepted=accepted,
        released=released,
    )
