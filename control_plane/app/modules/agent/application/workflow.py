from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.domain import (
    WorkflowClaimMode,
    WorkflowCommand,
    WorkflowCommandKind,
    WorkflowCommandState,
    WorkflowDispatchMutation,
)

_CLAIM_LEASE = timedelta(seconds=30)
_MAX_CLAIMS = 3


class WorkflowOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    ACKNOWLEDGEMENT_UNKNOWN = "ACKNOWLEDGEMENT_UNKNOWN"


class WorkflowLookupOutcome(StrEnum):
    NEVER_OBSERVED = "NEVER_OBSERVED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    STILL_UNKNOWN = "STILL_UNKNOWN"


class WorkflowErrorCode(StrEnum):
    DEV_DETERMINISTIC_REJECTION = "DEV_DETERMINISTIC_REJECTION"
    WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN = "WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN"
    WORKFLOW_PRE_CALL_FAILURE = "WORKFLOW_PRE_CALL_FAILURE"
    WORKFLOW_RECONCILIATION_REJECTED = "WORKFLOW_RECONCILIATION_REJECTED"
    WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED = "WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED"


class WorkflowCommandRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    command_key: str = Field(serialization_alias="commandKey")
    kind: WorkflowCommandKind
    attempt_id: str = Field(serialization_alias="attemptId")
    generation: int = Field(ge=1)


class WorkflowReceipt(BaseModel):
    """Only accepted platform acknowledgement facts, never claim metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    command_key: str = Field(min_length=1, serialization_alias="commandKey")
    outcome: WorkflowOutcome

    @model_validator(mode="after")
    def accepted_only(self) -> WorkflowReceipt:
        if self.outcome is not WorkflowOutcome.ACCEPTED:
            raise ValueError("workflow receipts can contain only ACCEPTED acknowledgements")
        return self


class WorkflowDispatchOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: WorkflowOutcome
    receipt: WorkflowReceipt | None = None
    error_code: WorkflowErrorCode | None = Field(default=None, serialization_alias="errorCode")

    @model_validator(mode="after")
    def accepted_requires_receipt(self) -> WorkflowDispatchOutcome:
        if (self.outcome is WorkflowOutcome.ACCEPTED) != (self.receipt is not None):
            raise ValueError("only ACCEPTED workflow outcomes carry a receipt")
        return self


class WorkflowLookupResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: WorkflowLookupOutcome
    receipt: WorkflowReceipt | None = None
    error_code: WorkflowErrorCode | None = Field(default=None, serialization_alias="errorCode")

    @model_validator(mode="after")
    def confirmed_requires_receipt(self) -> WorkflowLookupResult:
        if (self.outcome is WorkflowLookupOutcome.CONFIRMED) != (self.receipt is not None):
            raise ValueError("only CONFIRMED workflow lookups carry a receipt")
        return self


class WorkflowClaim(BaseModel):
    """Fenced platform claim columns, separate from orchestration receipts."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    owner: str = Field(min_length=1)
    token: str = Field(min_length=1)
    lease_until: datetime = Field(serialization_alias="leaseUntil")
    mode: WorkflowClaimMode


class WorkflowPreCallFailure(RuntimeError):
    pass


class WorkflowOrchestratorPort(Protocol):
    def start(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome: ...
    def cancel(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome: ...
    def resume(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome: ...
    def lookup(self, command_key: str) -> WorkflowLookupResult: ...


@dataclass(frozen=True, slots=True)
class WorkflowDispatchResult:
    claimed: int = 0
    dispatched: int = 0
    failed: int = 0
    unknown: int = 0
    pre_call_failures: int = 0


@dataclass(frozen=True, slots=True)
class WorkflowReconciliationResult:
    claimed: int = 0
    confirmed: int = 0
    rejected: int = 0
    still_unknown: int = 0
    never_observed: int = 0


def _request(command: WorkflowCommand) -> WorkflowCommandRequest:
    return WorkflowCommandRequest(
        command_key=command.command_key,
        kind=command.kind,
        attempt_id=command.attempt_id,
        generation=command.generation,
    )


def _receipt(command: WorkflowCommand, receipt: WorkflowReceipt | None) -> dict[str, object] | None:
    if (
        receipt is None
        or receipt.command_key != command.command_key
        or receipt.outcome is not WorkflowOutcome.ACCEPTED
    ):
        return None
    return receipt.model_dump(mode="json", by_alias=True)


def _park(dependencies: AgentDependencies) -> None:
    now = dependencies.clock()
    dependencies.transaction_runner(
        lambda uow: uow.repository().park_expired_workflow_claims(now=now)
    )


def _claim(
    dependencies: AgentDependencies, mode: WorkflowClaimMode, limit: int
) -> tuple[WorkflowCommand, ...]:
    now = dependencies.clock()
    claim = WorkflowClaim(
        owner=f"agent-workflow-{mode.value.casefold()}",
        token=dependencies.new_id(),
        lease_until=now + _CLAIM_LEASE,
        mode=mode,
    )
    return dependencies.transaction_runner(
        lambda uow: uow.repository().claim_workflow_commands(
            limit=limit,
            now=now,
            claim_owner=claim.owner,
            claim_token=claim.token,
            claim_lease_until=claim.lease_until,
            claim_mode=claim.mode,
        )
    )


def _persist(
    dependencies: AgentDependencies,
    command: WorkflowCommand,
    state: WorkflowCommandState,
    receipt: dict[str, object] | None,
    error: WorkflowErrorCode | None,
) -> WorkflowCommand | None:
    claim_token = command.claim_token
    if claim_token is None:
        raise RuntimeError("claimed workflow command is missing claim token")
    now = dependencies.clock()
    return dependencies.transaction_runner(
        lambda uow: uow.repository().record_workflow_command_dispatch(
            command.id,
            expected_state=command.state,
            expected_claim_token=claim_token,
            mutation=WorkflowDispatchMutation(
                state=state,
                receipt=receipt,
                error_code=error.value if error is not None else None,
                now=now,
            ),
        )
    )


def _uncertain(command: WorkflowCommand) -> tuple[WorkflowCommandState, WorkflowErrorCode]:
    if command.dispatch_attempts >= _MAX_CLAIMS:
        return WorkflowCommandState.UNKNOWN, WorkflowErrorCode.WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED
    return WorkflowCommandState.UNKNOWN, WorkflowErrorCode.WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN


def _dispatch(
    orchestrator: WorkflowOrchestratorPort, command: WorkflowCommand
) -> WorkflowDispatchOutcome:
    request = _request(command)
    if command.kind is WorkflowCommandKind.START:
        return orchestrator.start(request)
    if command.kind is WorkflowCommandKind.CANCEL:
        return orchestrator.cancel(request)
    return orchestrator.resume(request)


def dispatch_workflow_commands(
    *, limit: int, dependencies: AgentDependencies
) -> WorkflowDispatchResult:
    if limit < 1:
        raise ValueError("workflow dispatch limit must be positive")
    if dependencies.workflow_orchestrator is None:
        raise RuntimeError("workflow orchestrator is not configured")
    _park(dependencies)
    claimed = _claim(dependencies, WorkflowClaimMode.DISPATCH, limit)
    dispatched = failed = unknown = pre_call_failures = 0
    for command in claimed:
        try:
            outcome = _dispatch(dependencies.workflow_orchestrator, command)
        except WorkflowPreCallFailure:
            state = (
                WorkflowCommandState.UNKNOWN
                if command.dispatch_attempts >= _MAX_CLAIMS
                else WorkflowCommandState.PLANNED
            )
            pre_call_error = (
                WorkflowErrorCode.WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED
                if state is WorkflowCommandState.UNKNOWN
                else WorkflowErrorCode.WORKFLOW_PRE_CALL_FAILURE
            )
            pre_call_failures += (
                _persist(dependencies, command, state, None, pre_call_error) is not None
            )
            continue
        except Exception:
            state, error = _uncertain(command)
            unknown += _persist(dependencies, command, state, None, error) is not None
            continue
        if outcome.outcome is WorkflowOutcome.ACCEPTED:
            receipt = _receipt(command, outcome.receipt)
            if receipt is not None:
                dispatched += (
                    _persist(dependencies, command, WorkflowCommandState.DISPATCHED, receipt, None)
                    is not None
                )
            else:
                state, error = _uncertain(command)
                unknown += _persist(dependencies, command, state, None, error) is not None
        elif outcome.outcome is WorkflowOutcome.REJECTED:
            failed += (
                _persist(
                    dependencies,
                    command,
                    WorkflowCommandState.FAILED,
                    None,
                    WorkflowErrorCode.DEV_DETERMINISTIC_REJECTION,
                )
                is not None
            )
        else:
            state, error = _uncertain(command)
            unknown += _persist(dependencies, command, state, None, error) is not None
    return WorkflowDispatchResult(len(claimed), dispatched, failed, unknown, pre_call_failures)


def reconcile_workflow_commands(
    *, limit: int, dependencies: AgentDependencies
) -> WorkflowReconciliationResult:
    if limit < 1:
        raise ValueError("workflow reconciliation limit must be positive")
    if dependencies.workflow_orchestrator is None:
        raise RuntimeError("workflow orchestrator is not configured")
    _park(dependencies)
    claimed = _claim(dependencies, WorkflowClaimMode.RECONCILE, limit)
    confirmed = rejected = still_unknown = never_observed = 0
    for command in claimed:
        try:
            outcome = dependencies.workflow_orchestrator.lookup(command.command_key)
        except Exception:
            outcome = WorkflowLookupResult(outcome=WorkflowLookupOutcome.STILL_UNKNOWN)
        if outcome.outcome is WorkflowLookupOutcome.CONFIRMED:
            receipt = _receipt(command, outcome.receipt)
            if receipt is not None:
                confirmed += (
                    _persist(dependencies, command, WorkflowCommandState.DISPATCHED, receipt, None)
                    is not None
                )
            else:
                state, error = _uncertain(command)
                still_unknown += _persist(dependencies, command, state, None, error) is not None
        elif outcome.outcome is WorkflowLookupOutcome.REJECTED:
            rejected += (
                _persist(
                    dependencies,
                    command,
                    WorkflowCommandState.FAILED,
                    None,
                    WorkflowErrorCode.WORKFLOW_RECONCILIATION_REJECTED,
                )
                is not None
            )
        elif outcome.outcome is WorkflowLookupOutcome.NEVER_OBSERVED:
            if (
                command.state is WorkflowCommandState.PLANNED
                and command.dispatch_attempts < _MAX_CLAIMS
            ):
                state = WorkflowCommandState.PLANNED
                never_observed_error = None
            else:
                state, never_observed_error = _uncertain(command)
            never_observed += (
                _persist(dependencies, command, state, None, never_observed_error) is not None
            )
        else:
            state, error = _uncertain(command)
            still_unknown += _persist(dependencies, command, state, None, error) is not None
    return WorkflowReconciliationResult(
        len(claimed), confirmed, rejected, still_unknown, never_observed
    )
