from datetime import datetime
from typing import Literal, Self

from pydantic import Field

from control_plane.app.modules.model_gateway.domain.connections import digest
from control_plane.app.modules.model_gateway.domain.metadata import (
    MetadataAdmission,
    MetadataApplicability,
    MetadataInput,
    MetadataLimitComparison,
    MetadataModel,
    MetadataObservation,
    MetadataReason,
    MetadataReport,
    MetadataState,
    Sha256,
    compare_limit,
)


class CreateMetadataObservationRequestDto(MetadataModel):
    source_fingerprint: Sha256


class MetadataObservationReceiptDto(MetadataModel):
    id: str
    deployment_id: str
    revision: Literal[1] = 1
    candidate_revision: int
    source_fingerprint: Sha256
    input_digest: Sha256
    requested_at: datetime
    state: Literal["QUEUED"] = "QUEUED"

    @classmethod
    def from_observation(cls, value: MetadataObservation) -> Self:
        return cls(
            id=value.id,
            deployment_id=value.deployment_id,
            candidate_revision=value.input.deployment_revision,
            source_fingerprint=value.input.source.fingerprint,
            input_digest=value.input.input_digest,
            requested_at=value.requested_at,
        )


def result_hash(value: MetadataObservation) -> str | None:
    if value.state in {MetadataState.QUEUED, MetadataState.RUNNING}:
        return None
    return digest(value.model_dump(mode="json", exclude={"execution_token", "deadline_at"}))


class MetadataHistoryItemDto(MetadataModel):
    id: str
    deployment_id: str
    revision: int
    candidate_revision: int
    requested_at: datetime
    state: MetadataState
    input_digest: Sha256
    result_hash: Sha256 | None

    @classmethod
    def from_observation(cls, value: MetadataObservation) -> Self:
        return cls(
            id=value.id,
            deployment_id=value.deployment_id,
            revision=value.revision,
            candidate_revision=value.input.deployment_revision,
            requested_at=value.requested_at,
            state=value.state,
            input_digest=value.input.input_digest,
            result_hash=result_hash(value),
        )


class MetadataObservationListDto(MetadataModel):
    items: list[MetadataHistoryItemDto]
    next_cursor: str | None
    candidate_revision: int
    admission: MetadataAdmission


class MetadataObservationDto(MetadataModel):
    id: str
    deployment_id: str
    revision: int
    requested_by: str
    requested_at: datetime
    input: MetadataInput
    state: MetadataState
    reason: MetadataReason | None
    started_at: datetime | None
    finished_at: datetime | None
    elapsed_ms: int | None
    response_sha256: Sha256 | None
    response_bytes: int | None
    provider_request_id: str | None
    report: MetadataReport | None
    result_hash: Sha256 | None
    context_window_comparison: MetadataLimitComparison | None
    max_output_tokens_comparison: MetadataLimitComparison | None
    applicability: MetadataApplicability = Field(
        description=(
            "Only known input/configuration binding. Not current Provider content, "
            "account permission or private Secret validity."
        )
    )
    applicability_reasons: list[str]

    @classmethod
    def from_observation(
        cls, value: MetadataObservation, applicability: MetadataApplicability, reasons: list[str]
    ) -> Self:
        return cls.model_validate(
            value.model_dump(
                exclude={
                    "attempt",
                    "execution_token",
                    "deadline_at",
                    "material_currentness",
                }
            )
            | {
                "result_hash": result_hash(value),
                "applicability": applicability,
                "applicability_reasons": reasons,
                "context_window_comparison": compare_limit(
                    value.report.context_window, value.input.declared_context_window
                )
                if value.report
                else None,
                "max_output_tokens_comparison": compare_limit(
                    value.report.max_output_tokens, value.input.declared_max_output_tokens
                )
                if value.report
                else None,
            }
        )
