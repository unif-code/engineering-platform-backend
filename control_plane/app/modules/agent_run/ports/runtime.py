from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import AwareDatetime, ConfigDict, model_validator

from control_plane.app.modules.agent_run.domain import (
    EvidenceRef,
    ExecutionBindingProjection,
    MaterializationGuard,
    MaterializationHandle,
)
from control_plane.app.modules.agent_run.domain.models import FrozenModel, NonEmptyStr, PositiveInt


class RuntimeMaterializationError(RuntimeError):
    """Retryable failure while applying a runtime materialization step."""


class RuntimeMaterializationRequest(FrozenModel):
    operation_id: NonEmptyStr
    handle: MaterializationHandle
    binding: ExecutionBindingProjection


class RuntimeReadiness(FrozenModel):
    materialization_id: NonEmptyStr
    binding_digest: NonEmptyStr
    generation: PositiveInt
    protocol_version: NonEmptyStr
    deadline_at: AwareDatetime
    lab_only: Literal[True] = True
    isolation_evidence_refs: tuple[EvidenceRef, ...] = ()


class RuntimePreview(FrozenModel):
    preview_id: NonEmptyStr
    access_ref: NonEmptyStr
    expires_at: AwareDatetime


class RuntimePresence(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


class RuntimeObservation(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    materialization_id: NonEmptyStr
    presence: RuntimePresence
    generation: PositiveInt | None = None
    evidence_persisted: bool = False
    side_effects_fenced: bool = False
    secret_revoked: bool = False
    destroyed: bool = False
    preview_access_active: bool = False

    @model_validator(mode="after")
    def validate_presence(self) -> "RuntimeObservation":
        if self.presence is RuntimePresence.PRESENT and self.generation is None:
            raise ValueError("present runtime observation requires a generation")
        if self.presence is not RuntimePresence.PRESENT and (
            self.generation is not None
            or self.evidence_persisted
            or self.side_effects_fenced
            or self.secret_revoked
            or self.destroyed
            or self.preview_access_active
        ):
            raise ValueError("non-present runtime observation cannot assert runtime state")
        return self


class RuntimeMaterializerPort(Protocol):
    def provision(self, request: RuntimeMaterializationRequest) -> RuntimeReadiness: ...

    def publish_preview(
        self,
        operation_id: str,
        guard: MaterializationGuard,
        metadata: EvidenceRef,
        expires_at: datetime,
    ) -> RuntimePreview: ...

    def persist_evidence(
        self,
        guard: MaterializationGuard,
        evidence_refs: tuple[EvidenceRef, ...],
    ) -> None: ...

    def fence(self, guard: MaterializationGuard) -> None: ...

    def revoke_secret(self, guard: MaterializationGuard) -> None: ...

    def destroy(self, guard: MaterializationGuard) -> None: ...

    def observe(self, materialization_id: str) -> RuntimeObservation: ...
