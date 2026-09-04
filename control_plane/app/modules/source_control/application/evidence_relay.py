from datetime import timedelta

from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.application.evidence import (
    accept_external_validation,
    accept_integration_baseline_request,
    record_external_validation_rejection,
)
from control_plane.app.modules.source_control.domain import (
    EvidenceMessageConflict,
    EvidenceStale,
    ExternalValidationRequestEnvelope,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.ports import RelayEvidenceRequestsResult


def relay_requirement_evidence_requests(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> RelayEvidenceRequestsResult:
    requirement = dependencies.requirement_evidence
    repository_factory = dependencies.evidence_repository_factory
    if requirement is None or repository_factory is None:
        raise SourceControlDependencyUnavailable("Evidence relay dependency unavailable")
    now = dependencies.clock.now()
    try:
        messages = requirement.claim_requests(
            limit=limit,
            lease_until=now + timedelta(seconds=30),
        )
    except Exception:
        raise RequirementCallbackUnavailable("Requirement Evidence claim unavailable") from None
    accepted = 0
    released = 0
    for message in messages:
        try:
            with dependencies.engine.begin() as db:
                repository = repository_factory(db)
                if isinstance(message, ExternalValidationRequestEnvelope):
                    try:
                        accept_external_validation(
                            repository,
                            message,
                            dependencies=dependencies,
                        )
                    except EvidenceStale:
                        record_external_validation_rejection(
                            repository,
                            message,
                            dependencies=dependencies,
                        )
                else:
                    accept_integration_baseline_request(
                        repository,
                        message,
                        dependencies=dependencies,
                    )
        except EvidenceMessageConflict:
            try:
                requirement.release_request(
                    message.message_id,
                    error_code="EVIDENCE_REQUEST_CONFLICT",
                    retry_at=now + timedelta(minutes=5),
                )
            except Exception:
                raise RequirementCallbackUnavailable(
                    "Requirement Evidence release unavailable"
                ) from None
            released += 1
            continue
        except Exception:
            try:
                requirement.release_request(
                    message.message_id,
                    error_code="SOURCE_CONTROL_UNAVAILABLE",
                    retry_at=now + timedelta(minutes=5),
                )
            except Exception:
                raise RequirementCallbackUnavailable(
                    "Requirement Evidence release unavailable"
                ) from None
            released += 1
            continue
        try:
            requirement.acknowledge_request(message.message_id)
        except Exception:
            raise RequirementCallbackUnavailable(
                "Requirement Evidence acknowledgement unavailable"
            ) from None
        accepted += 1
    return RelayEvidenceRequestsResult(
        claimed=len(messages),
        accepted=accepted,
        released=released,
    )
