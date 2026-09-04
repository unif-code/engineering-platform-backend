from datetime import datetime
from typing import Any, Protocol

from pydantic import AwareDatetime, Field

from control_plane.app.modules.agent_run.domain import (
    EvidenceRef,
    ExecutionBindingProjection,
    MaterializationGuard,
    MaterializationState,
    MaterializationStatus,
    SandboxEnvironmentRef,
)
from control_plane.app.modules.agent_run.domain.models import (
    FrozenModel,
    NonEmptyStr,
    PositiveInt,
)


class AdmissionPolicySnapshot(FrozenModel):
    policy_version: NonEmptyStr
    enabled: bool
    active_attempt_limit: PositiveInt
    maximum_units: PositiveInt
    lease_ttl_seconds: int = Field(ge=1, le=86400)


class ReservationRecord(FrozenModel):
    materialization_id: NonEmptyStr
    environment_id: NonEmptyStr
    execution_id: NonEmptyStr
    lease_id: NonEmptyStr
    generation: PositiveInt
    revision: PositiveInt
    binding_digest: NonEmptyStr
    deadline_at: AwareDatetime


class ProvisionRecovery(FrozenModel):
    reservation: ReservationRecord
    recovery_capsule: bytes


class LockedMaterialization(FrozenModel):
    materialization_id: NonEmptyStr
    environment_id: NonEmptyStr
    execution_id: NonEmptyStr
    lease_id: NonEmptyStr
    generation: PositiveInt
    revision: PositiveInt
    state: NonEmptyStr
    binding_digest: NonEmptyStr
    deadline_at: AwareDatetime


class CleanupRecord(FrozenModel):
    materialization_id: NonEmptyStr
    environment_id: NonEmptyStr
    execution_id: NonEmptyStr
    lease_id: NonEmptyStr
    generation: PositiveInt
    revision: PositiveInt
    state: MaterializationState
    deadline_at: AwareDatetime
    lease_expires_at: AwareDatetime
    cleanup_terminal_state: MaterializationState | None
    cancellation_reason: NonEmptyStr | None
    evidence_persisted: bool
    fenced: bool
    secret_revoked: bool
    lease_released: bool
    destroyed: bool


class CommandOwnership(FrozenModel):
    command_id: NonEmptyStr
    owner_id: NonEmptyStr | None
    state: NonEmptyStr
    phase: NonEmptyStr
    subject_id: NonEmptyStr | None = None
    created: bool
    taken_over: bool


class PreviewIntent(FrozenModel):
    command_id: NonEmptyStr
    materialization_id: NonEmptyStr
    generation: PositiveInt
    revision: PositiveInt
    state: NonEmptyStr


class SandboxRepository(Protocol):
    def save_command_progress(
        self,
        command_id: str,
        *,
        owner_id: str,
        progress: dict[str, Any],
        now: datetime,
    ) -> None: ...

    def acquire_command(self, **values: Any) -> CommandOwnership: ...

    def advance_command(
        self,
        record_id: str,
        *,
        owner_id: str,
        phase: str,
        subject_id: str | None,
        now: datetime,
    ) -> None: ...

    def claim_idempotency(self, **values: Any) -> bool: ...

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def complete_idempotency(
        self,
        record_id: str,
        *,
        owner_id: str | None = None,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool: ...

    def ensure_environment_and_capacity(
        self,
        environment: SandboxEnvironmentRef,
        policy: AdmissionPolicySnapshot,
        *,
        now: datetime,
    ) -> None: ...

    def reserve_materialization(
        self,
        *,
        binding: ExecutionBindingProjection,
        policy: AdmissionPolicySnapshot,
        materialization_id: str,
        lease_id: str,
        generation_id: str,
        fencing_token_digest: str,
        now: datetime,
    ) -> ReservationRecord: ...

    def lock_current_guard(self, guard: MaterializationGuard) -> LockedMaterialization: ...

    def bind_command_subject(
        self,
        record_id: str,
        materialization_id: str,
        *,
        owner_id: str,
        phase: str,
        recovery_capsule: bytes,
        now: datetime,
    ) -> None: ...

    def provision_recovery(self, materialization_id: str) -> ProvisionRecovery: ...

    def mark_materialization_ready(
        self,
        materialization_id: str,
        *,
        generation: int,
        now: datetime,
    ) -> ReservationRecord: ...

    def materialization_status(self, materialization_id: str) -> MaterializationStatus: ...

    def environment_for_materialization(self, materialization_id: str) -> str: ...

    def environment_for_execution(self, execution_id: str) -> str: ...

    def cleanup_record(self, materialization_id: str) -> CleanupRecord: ...

    def cleanup_candidates(
        self,
        *,
        environment_id: str,
        execution_id: str | None,
        observed_at: datetime,
    ) -> tuple[CleanupRecord, ...]: ...

    def begin_preview_intent(
        self,
        *,
        command_id: str,
        owner_id: str,
        guard: MaterializationGuard,
        metadata: EvidenceRef,
        expires_at: datetime,
        now: datetime,
    ) -> PreviewIntent: ...

    def complete_preview_intent(
        self,
        *,
        command_id: str,
        owner_id: str,
        evidence_id: str,
        metadata: EvidenceRef,
        result_capsule: bytes,
        now: datetime,
    ) -> PreviewIntent: ...

    def begin_cleanup(
        self,
        guard: MaterializationGuard,
        *,
        evidence: tuple[tuple[str, EvidenceRef], ...],
        transition_state: str,
        terminal_state: str,
        cancellation_reason: str | None,
        now: datetime,
    ) -> int: ...

    def begin_cancel_cleanup(
        self,
        *,
        execution_id: str,
        authorized_environment_id: str,
        terminal_state: str,
        cancellation_reason: str,
        command_id: str,
        owner_id: str,
        now: datetime,
    ) -> CleanupRecord: ...

    def begin_provision_cleanup(
        self,
        materialization_id: str,
        *,
        denial_code: str,
        failure_dimension: str,
        now: datetime,
    ) -> CleanupRecord: ...

    def mark_evidence_persisted(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> int: ...

    def mark_fenced(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> int: ...

    def mark_secret_revoked(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> int: ...

    def release_capacity(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> int: ...

    def complete_cleanup(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        terminal_state: str,
        now: datetime,
    ) -> int: ...

    def quarantine_cleanup(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        denial_code: str,
        failure_dimension: str,
        now: datetime,
    ) -> int: ...

    def record_reconciliation(
        self,
        *,
        reconciliation_id: str,
        environment_id: str,
        execution_id: str | None,
        actor: str,
        correlation_id: str,
        observed_at: datetime,
        scanned_count: int,
        reconciled_count: int,
        now: datetime,
    ) -> bool: ...
