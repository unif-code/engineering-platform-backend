# ruff: noqa: E501

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, TypeVar

from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentAuditAppend,
    AgentDefinition,
    AgentIdempotencyRecord,
    AgentRun,
    AgentRunListItem,
    AttemptMutation,
    CanonicalEventInput,
    CheckpointInput,
    EventAcceptanceReceipt,
    ExecutionBinding,
    IdempotencyCompletion,
    IdempotencyReservation,
    RunMutation,
    RunState,
    WorkflowClaimMode,
    WorkflowCommand,
    WorkflowCommandState,
    WorkflowDispatchMutation,
)

T = TypeVar("T")


class AgentRepository(Protocol):
    """Persistence operations over frozen platform Agent DTOs only."""

    def insert_definition(self, definition: AgentDefinition) -> AgentDefinition: ...

    def definition_by_id(self, definition_id: str, version: int) -> AgentDefinition | None: ...

    def list_definitions(self) -> tuple[AgentDefinition, ...]: ...

    def insert_run(self, run: AgentRun) -> AgentRun: ...

    def run_by_id(self, run_id: str, *, for_update: bool = False) -> AgentRun | None: ...

    def runs_page(
        self,
        workspace_id: str,
        *,
        state: RunState | None,
        before_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> tuple[AgentRunListItem, ...]: ...

    def compare_and_set_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        mutation: RunMutation,
    ) -> AgentRun | None: ...

    def insert_attempt(self, attempt: AgentAttempt) -> AgentAttempt: ...

    def attempt_by_id(
        self, attempt_id: str, *, for_update: bool = False
    ) -> AgentAttempt | None: ...

    def attempts_by_run_id(self, run_id: str) -> tuple[AgentAttempt, ...]: ...

    def compare_and_set_attempt(
        self,
        attempt_id: str,
        *,
        expected_revision: int,
        mutation: AttemptMutation,
    ) -> AgentAttempt | None: ...

    def insert_binding(self, attempt_id: str, binding: ExecutionBinding) -> ExecutionBinding: ...

    def binding_by_attempt_id(self, attempt_id: str) -> ExecutionBinding | None: ...

    def append_event(self, event: CanonicalEventInput) -> CanonicalEventInput: ...

    def event_by_id(self, event_id: str) -> CanonicalEventInput | None: ...

    def append_event_receipt(self, receipt: EventAcceptanceReceipt) -> None: ...

    def event_receipt_by_id(self, event_id: str) -> EventAcceptanceReceipt | None: ...

    def events_page_by_run_id(
        self,
        run_id: str,
        *,
        after_event_id: str | None,
        limit: int,
    ) -> tuple[CanonicalEventInput, ...]: ...

    def insert_checkpoint(
        self, attempt_id: str, checkpoint: CheckpointInput
    ) -> CheckpointInput: ...

    def checkpoint_by_id(self, checkpoint_id: str) -> CheckpointInput | None: ...

    def insert_workflow_command(self, command: WorkflowCommand) -> WorkflowCommand: ...

    def workflow_command_by_id(
        self,
        command_id: str,
        *,
        for_update: bool = False,
    ) -> WorkflowCommand | None: ...

    def claim_workflow_commands(
        self,
        *,
        limit: int,
        now: datetime,
        claim_owner: str,
        claim_token: str,
        claim_lease_until: datetime,
        claim_mode: WorkflowClaimMode,
    ) -> tuple[WorkflowCommand, ...]: ...

    def park_expired_workflow_claims(self, *, now: datetime) -> int: ...

    def record_workflow_command_dispatch(
        self,
        command_id: str,
        *,
        expected_state: WorkflowCommandState,
        expected_claim_token: str,
        mutation: WorkflowDispatchMutation,
    ) -> WorkflowCommand | None: ...

    def reserve_idempotency(self, record: AgentIdempotencyRecord) -> IdempotencyReservation: ...

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> AgentIdempotencyRecord | None: ...

    def complete_idempotency(
        self,
        record_id: str,
        *,
        completion: IdempotencyCompletion,
    ) -> AgentIdempotencyRecord | None: ...


class AgentUnitOfWork(Protocol):
    """Platform-owned transaction boundary; adapters keep database handles private."""

    def repository(self) -> AgentRepository: ...

    def append_audit_event(self, event: AgentAuditAppend) -> None: ...


class AgentRepositoryFactory(Protocol):
    """Composition seam for a platform Agent unit of work."""

    def __call__(self) -> AgentUnitOfWork: ...


class AgentTransactionRunner(Protocol):
    def __call__(self, operation: Callable[[AgentUnitOfWork], T]) -> T: ...
