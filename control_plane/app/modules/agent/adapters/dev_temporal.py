from __future__ import annotations

from threading import Lock

from control_plane.app.modules.agent.application.workflow import (
    WorkflowCommandRequest,
    WorkflowDispatchOutcome,
    WorkflowErrorCode,
    WorkflowLookupOutcome,
    WorkflowLookupResult,
    WorkflowOutcome,
    WorkflowPreCallFailure,
    WorkflowReceipt,
)


class DevTemporalAdapter:
    """Restricted local adapter: deterministic in-memory facts and no external side effects."""

    def __init__(self) -> None:
        self.command_keys: list[str] = []
        self.lookup_keys: list[str] = []
        self.reject_keys: set[str] = set()
        self.fail_after_accept = False
        self.fail_before_call = False
        self.lookup_unknown = False
        self.lookup_never_observed = False
        self._lock = Lock()
        self._outcomes: dict[str, WorkflowOutcome] = {}

    def start(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        return self._dispatch(command)

    def cancel(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        return self._dispatch(command)

    def resume(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        return self._dispatch(command)

    def lookup(self, command_key: str) -> WorkflowLookupResult:
        with self._lock:
            self.lookup_keys.append(command_key)
            outcome = self._outcomes.get(command_key)
        if self.lookup_never_observed and outcome is None:
            return WorkflowLookupResult(outcome=WorkflowLookupOutcome.NEVER_OBSERVED)
        if self.lookup_unknown or outcome is None:
            return WorkflowLookupResult(outcome=WorkflowLookupOutcome.STILL_UNKNOWN)
        if outcome is WorkflowOutcome.REJECTED:
            return WorkflowLookupResult(
                outcome=WorkflowLookupOutcome.REJECTED,
                error_code=WorkflowErrorCode.DEV_DETERMINISTIC_REJECTION,
            )
        return WorkflowLookupResult(
            outcome=WorkflowLookupOutcome.CONFIRMED,
            receipt=WorkflowReceipt(command_key=command_key, outcome=WorkflowOutcome.ACCEPTED),
        )

    def _dispatch(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        if self.fail_before_call:
            raise WorkflowPreCallFailure("DEV_PRE_CALL_FAILURE")
        with self._lock:
            existing = self._outcomes.get(command.command_key)
            if existing is None:
                self.command_keys.append(command.command_key)
                existing = (
                    WorkflowOutcome.REJECTED
                    if command.command_key in self.reject_keys
                    else WorkflowOutcome.ACCEPTED
                )
                self._outcomes[command.command_key] = existing
        if existing is WorkflowOutcome.REJECTED:
            return WorkflowDispatchOutcome(
                outcome=WorkflowOutcome.REJECTED,
                error_code=WorkflowErrorCode.DEV_DETERMINISTIC_REJECTION,
            )
        if self.fail_after_accept:
            return WorkflowDispatchOutcome(
                outcome=WorkflowOutcome.ACKNOWLEDGEMENT_UNKNOWN,
                error_code=WorkflowErrorCode.WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN,
            )
        return WorkflowDispatchOutcome(
            outcome=WorkflowOutcome.ACCEPTED,
            receipt=WorkflowReceipt(
                command_key=command.command_key,
                outcome=WorkflowOutcome.ACCEPTED,
            ),
        )
