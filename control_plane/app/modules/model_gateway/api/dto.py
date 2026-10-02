from datetime import datetime
from typing import Annotated

from pydantic import ConfigDict, Field

from control_plane.app.modules.model_gateway.domain import (
    ArchiveReason,
    CreateDeployment,
    Deployment,
    PatchDeployment,
)
from control_plane.app.modules.model_gateway.domain.checks import (
    BasicTextObservation,
    CheckInputSnapshot,
    CheckReason,
    CheckState,
    ConnectionCheck,
    CurrentnessReason,
    InputCurrentness,
    ProbeUsage,
    SearchQueryObservation,
    SearchSourceObservation,
    SearchSourceReference,
    StreamObservation,
    ThinkingObservation,
)
from control_plane.app.modules.model_gateway.domain.connections import CheckKind
from control_plane.app.shared.api.camel import CamelModel


class CreateModelDeploymentRequestDto(CreateDeployment, CamelModel):
    """Unverified candidate only.

    connectionRef accepts model-connection:<identifier>, never credentials or a URL.
    """

    model_config = ConfigDict(populate_by_name=False, extra="forbid")


class PatchModelDeploymentRequestDto(PatchDeployment, CamelModel):
    """Omitted fields stay unchanged; at least one editable field is required.

    Only connectionRef, description and the two token declarations accept null to clear.
    """

    model_config = ConfigDict(populate_by_name=False, extra="forbid")


class ArchiveModelDeploymentRequestDto(CamelModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False)
    reason: ArchiveReason


class ModelDeploymentDto(Deployment, CamelModel):
    """Persisted, unverified declaration.

    DRAFT does not imply availability, activation or routing eligibility.
    """


class ModelDeploymentsResponseDto(CamelModel):
    items: list[ModelDeploymentDto]
    next_cursor: str | None


class CreateConnectionCheckRequestDto(CamelModel):
    check_kind: CheckKind
    """Fixed server probe. No target, prompt, credentials or model parameters are writable."""

    model_config = ConfigDict(extra="forbid", populate_by_name=False)


class ConnectionCheckInputDto(CheckInputSnapshot, CamelModel):
    """Frozen non-secret input. inputDigest identifies the fixed probe and configuration."""


class ConnectionCheckUsageDto(ProbeUsage, CamelModel):
    """Only actual returned token metadata; missing values are unknown, not zero or free."""


class ConnectionCheckReceiptDto(CamelModel):
    """Historical admission receipt. Poll this check ID.

    Receipt revision is not a candidate revision.
    """

    id: str
    deployment_id: str
    candidate_revision: int
    check_kind: CheckKind
    revision: int
    state: CheckState
    reason: CheckReason | None
    requested_at: datetime

    @classmethod
    def from_check(cls, check: ConnectionCheck) -> "ConnectionCheckReceiptDto":
        return cls(
            id=check.id,
            deployment_id=check.deployment_id,
            candidate_revision=check.input.deployment_revision,
            check_kind=check.check_kind,
            revision=check.revision,
            state=check.state,
            reason=check.reason,
            requested_at=check.requested_at,
        )


class BasicTextObservationDto(BasicTextObservation, CamelModel):
    """Bounded basic-text observations; no response text is stored."""


class StreamObservationDto(StreamObservation, CamelModel):
    """Only local reception/closure facts.

    Provider cancellation and stopped billing are unconfirmed.
    """


class ThinkingObservationDto(ThinkingObservation, CamelModel):
    """Dedicated reasoning field observations, never reasoning text or a quality judgment.

    Counts and bytes are observed protocol metadata, not token usage or cost.
    """


class SearchSourceReferenceDto(SearchSourceReference, CamelModel):
    """Sanitized HTTPS reference reported by this search call.

    Query/fragment removed; never fetched and not a precise page snapshot.
    """


class SearchQueryObservationDto(SearchQueryObservation, CamelModel):
    """Count and digest of distinct normalized Provider queries.

    Original query text is absent; null metadata means the query was not returned.
    """


class SearchSourceObservationDto(SearchSourceObservation, CamelModel):
    """Only Provider-reported source signals, not verified pages, answer citations or quality.

    One HTTP attempt may contain multiple Provider searches; counts/limits are not billing caps.
    store=false covers response-session storage only, not a general zero-retention guarantee.
    """

    sources: tuple[SearchSourceReferenceDto, ...] = Field(max_length=32)
    queries: tuple[SearchQueryObservationDto, ...] = Field(max_length=8)


CheckObservationDto = Annotated[
    BasicTextObservationDto
    | StreamObservationDto
    | ThinkingObservationDto
    | SearchSourceObservationDto,
    Field(discriminator="kind"),
]


class ConnectionCheckDto(CamelModel):
    """SUCCEEDED has a kind-specific, limited meaning.

    BASIC_TEXT proves one complete basic text response. STREAM_TEXT proves valid text
    increments, normal completion and the final completion marker. STREAM_STOP proves
    text was observed and the local response stream/client were closed; Provider-side
    cancellation and stopped billing remain unconfirmed. THINKING proves nonblank dedicated
    reasoning, a complete answer and normal stream completion with local cleanup; it does not
    assess reasoning quality. THINKING_SIGNAL_MISSING means only that this complete response
    contained no dedicated reasoning signal, not that the model lacks thinking support.
    SEARCH_SOURCES proves completed native search calls, structured sanitized source references
    and a complete answer with local cleanup. It does not prove source accuracy, answer-to-source
    citation binding, remote search termination or billing limits. No kind activates a candidate.

    UNKNOWN may have executed and incurred Provider charges; never automatically resubmit.
    currentness is recomputed from the current candidate and non-secret connection/probe versions.
    """

    id: str
    deployment_id: str
    revision: int
    requested_by: str
    check_kind: CheckKind
    observation: CheckObservationDto | None
    requested_at: datetime
    input: ConnectionCheckInputDto
    state: CheckState
    reason: CheckReason | None
    attempt: int
    started_at: datetime | None
    deadline_at: datetime | None
    finished_at: datetime | None
    elapsed_ms: int | None
    provider_request_id: str | None
    reported_model_id: str | None
    usage: ConnectionCheckUsageDto | None
    currentness: InputCurrentness
    currentness_reasons: list[CurrentnessReason]

    @classmethod
    def from_check(
        cls, check: ConnectionCheck, currentness: InputCurrentness, reasons: list[CurrentnessReason]
    ) -> "ConnectionCheckDto":
        values = check.model_dump(
            exclude={"execution_token", "material_currentness", "input", "usage"}
        )
        return cls.model_validate(
            values
            | {
                "input": ConnectionCheckInputDto.model_validate(check.input.model_dump()),
                "usage": None
                if check.usage is None
                else ConnectionCheckUsageDto.model_validate(check.usage.model_dump()),
                "currentness": currentness,
                "currentness_reasons": reasons,
            }
        )


class ConnectionCheckListDto(CamelModel):
    items: list[ConnectionCheckDto]
    next_cursor: str | None
