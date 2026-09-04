from datetime import timedelta

from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.application.formal import (
    accept_formal_delivery_request,
)
from control_plane.app.modules.source_control.domain import (
    FormalDeliveryConflict,
    RelayFormalDeliveryRequestsResult,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
)


def relay_requirement_formal_delivery_requests(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> RelayFormalDeliveryRequestsResult:
    requirement = dependencies.requirement_formal_delivery
    factory = dependencies.formal_repository_factory
    if requirement is None or factory is None:
        raise SourceControlDependencyUnavailable("Formal Delivery relay unavailable")
    now = dependencies.clock.now()
    try:
        messages = requirement.claim_requests(
            limit=limit,
            lease_until=now + timedelta(seconds=30),
        )
    except Exception:
        raise RequirementCallbackUnavailable(
            "Requirement Formal Delivery claim unavailable"
        ) from None
    accepted = 0
    released = 0
    for message in messages:
        try:
            with dependencies.engine.begin() as db:
                accept_formal_delivery_request(
                    factory(db),
                    message,
                    dependencies=dependencies,
                )
        except FormalDeliveryConflict:
            error_code = "FORMAL_DELIVERY_CONFLICT"
        except Exception:
            error_code = "SOURCE_CONTROL_UNAVAILABLE"
        else:
            try:
                requirement.acknowledge_request(message.message_id)
            except Exception:
                raise RequirementCallbackUnavailable(
                    "Requirement Formal Delivery acknowledgement unavailable"
                ) from None
            accepted += 1
            continue
        try:
            requirement.release_request(
                message.message_id,
                error_code=error_code,
                retry_at=now + timedelta(minutes=5),
            )
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Formal Delivery release unavailable"
            ) from None
        released += 1
    return RelayFormalDeliveryRequestsResult(
        claimed=len(messages),
        accepted=accepted,
        released=released,
    )
