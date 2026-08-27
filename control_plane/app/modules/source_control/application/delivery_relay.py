from datetime import datetime, timedelta
from typing import Any

from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    DeliveryRequestEnvelope,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.ports import (
    RelayIntegrationDeliveryRequestsResult,
    RequirementDeliveryPort,
    SourceControlIntegrationRepository,
)

_CONFLICT_ERROR_CODE = "DELIVERY_REQUEST_CONFLICT"
_UNAVAILABLE_ERROR_CODE = "SOURCE_CONTROL_UNAVAILABLE"
_MAX_CONFLICT_RETRY_MINUTES = 24 * 60


class _DeliveryRequestMessageConflict(ValueError):
    pass


def _payload_hash(row: Any) -> str:
    return str(row["payload_hash"])


def _conflict_retry_at(envelope: DeliveryRequestEnvelope, *, now: datetime) -> datetime:
    exponent = min(envelope.attempts - 1, 11)
    delay_minutes = min(2**exponent, _MAX_CONFLICT_RETRY_MINUTES)
    return now + timedelta(minutes=delay_minutes)


def _accept_delivery_request(
    repository: SourceControlIntegrationRepository,
    envelope: DeliveryRequestEnvelope,
    *,
    now: datetime,
) -> None:
    existing = repository.delivery_request(envelope.message_id, for_update=True)
    if existing is not None:
        if _payload_hash(existing) != envelope.payload_hash:
            raise _DeliveryRequestMessageConflict
        return
    inserted = repository.accept_delivery_request(
        message_id=envelope.message_id,
        topic=envelope.topic,
        payload_hash=envelope.payload_hash,
        requirement_id=envelope.requirement_id,
        requirement_revision=envelope.requirement_revision,
        work_item_id=envelope.work_item_id,
        work_item_revision=envelope.work_item_revision,
        repository_id=envelope.repository_id,
        actor_id=envelope.actor_id,
        integration_merge_request_binding_id=(envelope.integration_merge_request_binding_id),
        now=now,
    )
    if inserted is not None:
        return
    existing = repository.delivery_request(envelope.message_id, for_update=True)
    if existing is None or _payload_hash(existing) != envelope.payload_hash:
        raise _DeliveryRequestMessageConflict


def _release_request(
    requirement: RequirementDeliveryPort,
    envelope: DeliveryRequestEnvelope,
    *,
    error_code: str,
    retry_at: datetime,
) -> None:
    try:
        requirement.release_request(
            envelope.message_id,
            error_code=error_code,
            retry_at=retry_at,
        )
    except Exception:
        raise RequirementCallbackUnavailable("Requirement release unavailable") from None


def relay_integration_delivery_requests(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> RelayIntegrationDeliveryRequestsResult:
    requirement = dependencies.requirement_delivery
    if requirement is None:
        raise SourceControlDependencyUnavailable("Requirement delivery dependency unavailable")
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Source Control delivery repository unavailable")
    now = dependencies.clock.now()
    try:
        messages = requirement.claim_requests(
            limit=limit,
            lease_until=now + timedelta(seconds=30),
        )
    except Exception:
        raise RequirementCallbackUnavailable("Requirement claim unavailable") from None
    accepted = 0
    released = 0
    for envelope in messages:
        try:
            with dependencies.engine.begin() as db:
                _accept_delivery_request(
                    repository_factory(db),
                    envelope,
                    now=now,
                )
        except _DeliveryRequestMessageConflict:
            _release_request(
                requirement,
                envelope,
                error_code=_CONFLICT_ERROR_CODE,
                retry_at=_conflict_retry_at(envelope, now=now),
            )
            released += 1
            continue
        except Exception:
            _release_request(
                requirement,
                envelope,
                error_code=_UNAVAILABLE_ERROR_CODE,
                retry_at=now + timedelta(minutes=5),
            )
            released += 1
            continue
        try:
            requirement.acknowledge_request(envelope.message_id)
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement acknowledgement unavailable"
            ) from None
        accepted += 1
    return RelayIntegrationDeliveryRequestsResult(
        claimed=len(messages),
        accepted=accepted,
        released=released,
    )
