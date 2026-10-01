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
    StreamObservation,
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


CheckObservationDto = Annotated[
    BasicTextObservationDto | StreamObservationDto, Field(discriminator="kind")
]


class ConnectionCheckDto(CamelModel):
    """SUCCEEDED proves one basic text response only.

    It never activates or fully verifies a candidate.

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
