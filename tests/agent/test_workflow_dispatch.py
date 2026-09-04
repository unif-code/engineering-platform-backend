from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from threading import Barrier, Event, Lock, Thread
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import text

from control_plane.app.modules.agent import (
    dispatch_workflow_commands,
    reconcile_workflow_commands,
    start_run,
)
from control_plane.app.modules.agent.adapters.dev_temporal import DevTemporalAdapter
from control_plane.app.modules.agent.adapters.sqlalchemy import (
    SqlAlchemyAgentRepository,
    SqlAlchemyAgentTransactionRunner,
)
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.runs import StartRunResult
from control_plane.app.modules.agent.application.workflow import (
    WorkflowCommandRequest,
    WorkflowDispatchOutcome,
    WorkflowLookupOutcome,
    WorkflowLookupResult,
    WorkflowOutcome,
    WorkflowReceipt,
)
from control_plane.app.modules.agent.domain import (
    WorkflowClaimMode,
    WorkflowCommand,
    WorkflowCommandKind,
    WorkflowCommandState,
)
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_start_run import NOW, _command, _dependencies


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    db: IsolatedAgentDatabase
    dependencies: AgentDependencies
    workflow: DevTemporalAdapter
    started: StartRunResult


def runtime(database: IsolatedAgentDatabase) -> AgentRuntime:
    workflow = DevTemporalAdapter()
    dependencies = replace(_dependencies(database), workflow_orchestrator=workflow)
    started = start_run(None, command=_command(), dependencies=dependencies)
    return AgentRuntime(
        db=database,
        dependencies=dependencies,
        workflow=workflow,
        started=started,
    )


def command_state(runtime: AgentRuntime) -> tuple[str, int, object | None, str | None]:
    with runtime.db.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT state, dispatch_attempts, receipt, last_error_code "
                "FROM agent.workflow_command WHERE id=CAST(:id AS UUID)"
            ),
            {"id": runtime.started.command.id},
        ).one()
    return tuple(row)


def command_record(runtime: AgentRuntime) -> dict[str, Any]:
    with runtime.db.owner.connect() as db:
        row = (
            db.execute(
                text(
                    "SELECT to_jsonb(workflow_command) AS command "
                    "FROM agent.workflow_command WHERE id=CAST(:id AS UUID)"
                ),
                {"id": runtime.started.command.id},
            )
            .mappings()
            .one()
        )
    return dict(row["command"])


class ScriptedWorkflow:
    def __init__(
        self,
        *,
        dispatch: WorkflowDispatchOutcome | None = None,
        lookup: WorkflowLookupResult | None = None,
    ) -> None:
        self.dispatch_outcome = dispatch
        self.lookup_outcome = lookup
        self.effect_requests: list[WorkflowCommandRequest] = []
        self.lookup_keys: list[str] = []

    def start(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        self.effect_requests.append(command)
        if self.dispatch_outcome is None:
            raise AssertionError("unexpected workflow effect call")
        return self.dispatch_outcome

    def cancel(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        return self.start(command)

    def resume(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        return self.start(command)

    def lookup(self, command_key: str) -> WorkflowLookupResult:
        self.lookup_keys.append(command_key)
        if self.lookup_outcome is None:
            raise AssertionError("unexpected workflow lookup")
        return self.lookup_outcome


class RouteRecordingWorkflow(DevTemporalAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.start_requests: list[WorkflowCommandRequest] = []
        self.cancel_requests: list[WorkflowCommandRequest] = []
        self.resume_requests: list[WorkflowCommandRequest] = []

    def start(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        self.start_requests.append(command)
        return super().start(command)

    def cancel(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        self.cancel_requests.append(command)
        return super().cancel(command)

    def resume(self, command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
        self.resume_requests.append(command)
        return super().resume(command)


class FailAfterPersistenceRunner:
    """Executes one mutation, then raises inside its real database transaction."""

    def __init__(self, database: IsolatedAgentDatabase, *, fail_call: int) -> None:
        self._delegate = SqlAlchemyAgentTransactionRunner(database.runtime)
        self._fail_call = fail_call
        self.calls = 0

    def __call__(self, operation: Callable[[Any], Any]) -> Any:
        self.calls += 1
        if self.calls != self._fail_call:
            return self._delegate(operation)

        def fail_after_write(uow: Any) -> Any:
            operation(uow)
            raise RuntimeError("SENSITIVE_PERSISTENCE_SENTINEL")

        return self._delegate(fail_after_write)


def insert_command(runtime: AgentRuntime, command: WorkflowCommand) -> WorkflowCommand:
    return runtime.dependencies.transaction_runner(
        lambda uow: uow.repository().insert_workflow_command(command)
    )


def test_accepted_dispatch_persists_only_platform_receipt(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)

    result = dispatch_workflow_commands(None, limit=10, dependencies=agent.dependencies)

    assert (result.dispatched, result.failed, result.unknown) == (1, 0, 0)
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    state, attempts, receipt, error = command_state(agent)
    assert (state, attempts, error) == ("DISPATCHED", 1, None)
    assert receipt == {
        "commandKey": agent.started.command.command_key,
        "outcome": "ACCEPTED",
    }


def test_deterministic_rejection_is_failed_without_an_acknowledged_effect(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    agent.workflow.reject_keys.add(agent.started.command.command_key)

    result = dispatch_workflow_commands(None, limit=10, dependencies=agent.dependencies)

    assert (result.dispatched, result.failed, result.unknown) == (0, 1, 0)
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    state, _attempts, receipt, error = command_state(agent)
    assert (state, receipt, error) == ("FAILED", None, "DEV_DETERMINISTIC_REJECTION")


def test_unknown_ack_is_not_reissued_and_reconciles_by_same_key(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    agent.workflow.fail_after_accept = True

    first = dispatch_workflow_commands(None, limit=10, dependencies=agent.dependencies)

    assert first.unknown == 1
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    agent.workflow.fail_after_accept = False
    reconciled = reconcile_workflow_commands(None, limit=10, dependencies=agent.dependencies)
    assert reconciled.confirmed == 1
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    assert command_state(agent)[0] == WorkflowCommandState.DISPATCHED.value


def test_duplicate_dispatch_does_not_reissue_an_accepted_command(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)

    first = dispatch_workflow_commands(None, limit=10, dependencies=agent.dependencies)
    second = dispatch_workflow_commands(None, limit=10, dependencies=agent.dependencies)

    assert (first.dispatched, second.claimed) == (1, 0)
    assert agent.workflow.command_keys == [agent.started.command.command_key]


def test_second_backend_skips_a_live_locked_claim_before_first_backend_releases(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    first_locked = Event()
    release_first = Event()
    second_finished = Event()
    evidence: dict[str, object] = {}
    errors: list[BaseException] = []

    def hold_first_claim() -> None:
        try:
            with agent.db.runtime.begin() as db:
                evidence["first_pid"] = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
                claimed = SqlAlchemyAgentRepository(db).claim_workflow_commands(
                    limit=1,
                    now=agent.dependencies.clock(),
                    claim_owner="locked-dispatcher",
                    claim_token="10000000-0000-0000-0000-000000009911",
                    claim_lease_until=agent.dependencies.clock() + timedelta(seconds=30),
                    claim_mode=WorkflowClaimMode.DISPATCH,
                )
                evidence["first_claimed"] = len(claimed)
                first_locked.set()
                evidence["released_by_test"] = release_first.wait(timeout=10)
        except BaseException as exc:
            errors.append(exc)
            first_locked.set()

    def skip_locked_claim() -> None:
        try:
            with agent.db.runtime.begin() as db:
                evidence["second_pid"] = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
                claimed = SqlAlchemyAgentRepository(db).claim_workflow_commands(
                    limit=1,
                    now=agent.dependencies.clock(),
                    claim_owner="skipping-dispatcher",
                    claim_token="10000000-0000-0000-0000-000000009912",
                    claim_lease_until=agent.dependencies.clock() + timedelta(seconds=30),
                    claim_mode=WorkflowClaimMode.DISPATCH,
                )
                evidence["second_claimed"] = len(claimed)
                evidence["second_blockers"] = db.execute(
                    text("SELECT pg_blocking_pids(pg_backend_pid())")
                ).scalar_one()
        except BaseException as exc:
            errors.append(exc)
        finally:
            second_finished.set()

    first = Thread(target=hold_first_claim)
    first.start()
    assert first_locked.wait(timeout=10)
    second = Thread(target=skip_locked_claim)
    second.start()
    try:
        assert second_finished.wait(timeout=10)
        assert first.is_alive()
        assert errors == []
        assert evidence["first_claimed"] == 1
        assert evidence["second_claimed"] == 0
        assert evidence["first_pid"] != evidence["second_pid"]
        assert evidence["second_blockers"] == []
    finally:
        release_first.set()
        first.join(timeout=10)
        second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    assert evidence["released_by_test"] is True


def test_lookup_can_remain_unknown_without_reissuing_the_command(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    agent.workflow.fail_after_accept = True
    dispatch_workflow_commands(None, limit=10, dependencies=agent.dependencies)
    agent.workflow.lookup_unknown = True

    result = reconcile_workflow_commands(None, limit=10, dependencies=agent.dependencies)

    assert (result.confirmed, result.rejected, result.still_unknown) == (0, 0, 1)
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    assert command_state(agent)[0] == WorkflowCommandState.UNKNOWN.value


def test_genuine_unknown_never_observed_remains_lookup_only_and_dispatch_claims_zero(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    agent.workflow.fail_after_accept = True
    first = dispatch_workflow_commands(None, limit=1, dependencies=agent.dependencies)
    assert first.unknown == 1
    scripted = ScriptedWorkflow(
        lookup=WorkflowLookupResult(outcome=WorkflowLookupOutcome.NEVER_OBSERVED)
    )
    dependencies = replace(agent.dependencies, workflow_orchestrator=scripted)

    reconciled = reconcile_workflow_commands(None, limit=1, dependencies=dependencies)
    subsequent_dispatch = dispatch_workflow_commands(None, limit=1, dependencies=dependencies)

    assert (reconciled.never_observed, subsequent_dispatch.claimed) == (1, 0)
    assert command_state(agent) == (
        WorkflowCommandState.UNKNOWN.value,
        2,
        None,
        "WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN",
    )
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    assert scripted.effect_requests == []
    assert scripted.lookup_keys == [agent.started.command.command_key]


def test_proven_pre_call_failure_releases_claim_for_a_later_dispatch(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    agent.workflow.fail_before_call = True

    first = dispatch_workflow_commands(None, limit=10, dependencies=agent.dependencies)

    assert (first.dispatched, first.pre_call_failures) == (0, 1)
    assert agent.workflow.command_keys == []
    assert command_state(agent)[0] == WorkflowCommandState.PLANNED.value
    agent.workflow.fail_before_call = False
    second = dispatch_workflow_commands(None, limit=10, dependencies=agent.dependencies)
    assert second.dispatched == 1
    assert agent.workflow.command_keys == [agent.started.command.command_key]


def test_accepted_outcome_requires_an_accepted_receipt() -> None:
    with pytest.raises(ValueError):
        WorkflowDispatchOutcome(outcome=WorkflowOutcome.ACCEPTED)
    with pytest.raises(ValueError):
        WorkflowReceipt(command_key="workflow-key", outcome=WorkflowOutcome.ACKNOWLEDGEMENT_UNKNOWN)


def test_expired_dispatch_claim_recovers_by_lookup_before_same_key_dispatch(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    claim = agent.dependencies.transaction_runner(
        lambda uow: uow.repository().claim_workflow_commands(
            limit=1,
            now=agent.dependencies.clock(),
            claim_owner="dispatcher-a",
            claim_token="10000000-0000-0000-0000-000000009901",
            claim_lease_until=agent.dependencies.clock() + timedelta(seconds=30),
            claim_mode=WorkflowClaimMode.DISPATCH,
        )
    )
    assert len(claim) == 1
    expired = replace(
        agent.dependencies, clock=lambda: agent.dependencies.clock() + timedelta(seconds=31)
    )

    recovered = reconcile_workflow_commands(None, limit=10, dependencies=expired)

    assert recovered.confirmed == 0
    assert recovered.still_unknown == 1
    assert agent.workflow.command_keys == []
    assert command_state(agent)[0] == WorkflowCommandState.UNKNOWN.value
    assert agent.workflow.lookup_keys == [agent.started.command.command_key]


def test_expired_pre_effect_claim_is_lookup_first_and_never_observed_replans_below_cap(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    claimed = agent.dependencies.transaction_runner(
        lambda uow: uow.repository().claim_workflow_commands(
            limit=1,
            now=agent.dependencies.clock(),
            claim_owner="crashed-dispatcher",
            claim_token="10000000-0000-0000-0000-000000009921",
            claim_lease_until=agent.dependencies.clock() + timedelta(seconds=30),
            claim_mode=WorkflowClaimMode.DISPATCH,
        )
    )
    assert len(claimed) == 1
    agent.workflow.lookup_never_observed = True
    expired = replace(
        agent.dependencies, clock=lambda: agent.dependencies.clock() + timedelta(seconds=31)
    )

    reconciled = reconcile_workflow_commands(None, limit=1, dependencies=expired)

    assert (reconciled.claimed, reconciled.never_observed) == (1, 1)
    assert command_state(agent)[:2] == (WorkflowCommandState.PLANNED.value, 1)
    assert agent.workflow.lookup_keys == [agent.started.command.command_key]
    assert agent.workflow.command_keys == []
    dispatched = dispatch_workflow_commands(None, limit=1, dependencies=expired)
    assert dispatched.dispatched == 1
    assert agent.workflow.command_keys == [agent.started.command.command_key]


@pytest.mark.parametrize(
    ("rejected", "reconciled_field", "expected_state", "expected_error"),
    [
        (False, "confirmed", "DISPATCHED", None),
        (True, "rejected", "FAILED", "WORKFLOW_RECONCILIATION_REJECTED"),
    ],
)
def test_adapter_result_then_persistence_failure_rolls_back_and_reconciles_without_reissue(
    isolated_agent_database: IsolatedAgentDatabase,
    rejected: bool,
    reconciled_field: str,
    expected_state: str,
    expected_error: str | None,
) -> None:
    agent = runtime(isolated_agent_database)
    if rejected:
        agent.workflow.reject_keys.add(agent.started.command.command_key)
    failing_runner = FailAfterPersistenceRunner(agent.db, fail_call=3)
    failing_dependencies = replace(agent.dependencies, transaction_runner=failing_runner)

    with pytest.raises(RuntimeError, match="SENSITIVE_PERSISTENCE_SENTINEL"):
        dispatch_workflow_commands(None, limit=1, dependencies=failing_dependencies)

    record_after_rollback = command_record(agent)
    assert record_after_rollback["state"] == WorkflowCommandState.PLANNED.value
    assert record_after_rollback["dispatch_attempts"] == 1
    assert record_after_rollback["receipt"] is None
    assert record_after_rollback["last_error_code"] is None
    assert record_after_rollback["claim_token"] is not None
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    assert "SENSITIVE_PERSISTENCE_SENTINEL" not in json.dumps(record_after_rollback, default=str)

    expired = replace(
        failing_dependencies,
        clock=lambda: agent.dependencies.clock() + timedelta(seconds=31),
    )
    reconciled = reconcile_workflow_commands(None, limit=1, dependencies=expired)

    assert getattr(reconciled, reconciled_field) == 1
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    state, attempts, receipt, error = command_state(agent)
    assert (state, attempts, error) == (expected_state, 1, expected_error)
    if rejected:
        assert receipt is None
    else:
        assert receipt == {
            "commandKey": agent.started.command.command_key,
            "outcome": WorkflowOutcome.ACCEPTED.value,
        }


def test_expired_reconciliation_claim_is_reclaimed_for_lookup_only(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    agent.workflow.fail_after_accept = True
    dispatch_workflow_commands(None, limit=1, dependencies=agent.dependencies)
    claimed = agent.dependencies.transaction_runner(
        lambda uow: uow.repository().claim_workflow_commands(
            limit=1,
            now=agent.dependencies.clock(),
            claim_owner="crashed-reconciler",
            claim_token="10000000-0000-0000-0000-000000009922",
            claim_lease_until=agent.dependencies.clock() + timedelta(seconds=30),
            claim_mode=WorkflowClaimMode.RECONCILE,
        )
    )
    assert len(claimed) == 1
    agent.workflow.fail_after_accept = False
    expired = replace(
        agent.dependencies, clock=lambda: agent.dependencies.clock() + timedelta(seconds=31)
    )

    reconciled = reconcile_workflow_commands(None, limit=1, dependencies=expired)

    assert (reconciled.claimed, reconciled.confirmed) == (1, 1)
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    assert agent.workflow.lookup_keys == [agent.started.command.command_key]
    assert command_state(agent)[:2] == (WorkflowCommandState.DISPATCHED.value, 2)


def test_third_reconciliation_claim_crash_remains_lookup_reclaimable_without_attempt_four(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    agent.workflow.fail_after_accept = True
    dispatch_workflow_commands(None, limit=1, dependencies=agent.dependencies)
    agent.workflow.lookup_unknown = True
    reconcile_workflow_commands(None, limit=1, dependencies=agent.dependencies)
    claimed = agent.dependencies.transaction_runner(
        lambda uow: uow.repository().claim_workflow_commands(
            limit=1,
            now=agent.dependencies.clock(),
            claim_owner="third-crashed-reconciler",
            claim_token="10000000-0000-0000-0000-000000009923",
            claim_lease_until=agent.dependencies.clock() + timedelta(seconds=30),
            claim_mode=WorkflowClaimMode.RECONCILE,
        )
    )
    assert len(claimed) == 1
    assert claimed[0].dispatch_attempts == 3
    agent.workflow.fail_after_accept = False
    agent.workflow.lookup_unknown = False
    expired = replace(
        agent.dependencies, clock=lambda: agent.dependencies.clock() + timedelta(seconds=31)
    )

    reconciled = reconcile_workflow_commands(None, limit=1, dependencies=expired)

    assert (reconciled.claimed, reconciled.confirmed) == (1, 1)
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    assert agent.workflow.lookup_keys == [
        agent.started.command.command_key,
        agent.started.command.command_key,
    ]
    assert command_state(agent)[:2] == (WorkflowCommandState.DISPATCHED.value, 3)


def test_upgraded_third_predecessor_claim_reconciles_by_key_without_effect_reissue(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    config = Config("alembic.ini")
    alembic_command.downgrade(config, "agent@0001_agent_control_plane")
    command_id = "10000000-0000-0000-0000-000000009968"
    command_key = "predecessor-upgraded-third-claim"
    with isolated_agent_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO agent.workflow_command "
                "(id, command_key, kind, attempt_id, generation, state, dispatch_attempts, "
                "receipt, last_error_code, created_at, updated_at) VALUES "
                "(CAST(:id AS UUID), :command_key, 'START', "
                "'20000000-0000-0000-0000-000000009968', 1, 'UNKNOWN', 3, "
                "CAST(:receipt AS JSONB), NULL, :fixture_time, :fixture_time)"
            ),
            {
                "id": command_id,
                "command_key": command_key,
                "receipt": '{"commandKey":"predecessor-upgraded-third-claim","outcome":"CLAIMED"}',
                "fixture_time": NOW,
            },
        )
    alembic_command.upgrade(config, "agent@head")
    scripted = ScriptedWorkflow(
        lookup=WorkflowLookupResult(
            outcome=WorkflowLookupOutcome.CONFIRMED,
            receipt=WorkflowReceipt(command_key=command_key, outcome=WorkflowOutcome.ACCEPTED),
        )
    )
    dependencies = replace(_dependencies(isolated_agent_database), workflow_orchestrator=scripted)

    reconciled = reconcile_workflow_commands(None, limit=1, dependencies=dependencies)
    subsequent_dispatch = dispatch_workflow_commands(None, limit=1, dependencies=dependencies)

    with isolated_agent_database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT state, dispatch_attempts, receipt, last_error_code, claim_token "
                "FROM agent.workflow_command WHERE id=CAST(:id AS UUID)"
            ),
            {"id": command_id},
        ).one()
    assert (reconciled.claimed, reconciled.confirmed, subsequent_dispatch.claimed) == (1, 1, 0)
    assert tuple(row) == (
        WorkflowCommandState.DISPATCHED.value,
        3,
        {"commandKey": command_key, "outcome": WorkflowOutcome.ACCEPTED.value},
        None,
        None,
    )
    assert scripted.lookup_keys == [command_key]
    assert scripted.effect_requests == []


def test_still_unknown_exhausts_three_claims_and_never_reissues_effect(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)
    agent.workflow.fail_after_accept = True
    dispatch_workflow_commands(None, limit=1, dependencies=agent.dependencies)
    agent.workflow.lookup_unknown = True

    second = reconcile_workflow_commands(None, limit=1, dependencies=agent.dependencies)
    third = reconcile_workflow_commands(None, limit=1, dependencies=agent.dependencies)
    exhausted_reconcile = reconcile_workflow_commands(
        None, limit=1, dependencies=agent.dependencies
    )
    exhausted_dispatch = dispatch_workflow_commands(None, limit=1, dependencies=agent.dependencies)

    assert (second.still_unknown, third.still_unknown) == (1, 1)
    assert (exhausted_reconcile.claimed, exhausted_dispatch.claimed) == (0, 0)
    state, attempts, receipt, error = command_state(agent)
    assert (state, attempts, receipt, error) == (
        WorkflowCommandState.UNKNOWN.value,
        3,
        None,
        "WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED",
    )
    assert agent.workflow.command_keys == [agent.started.command.command_key]
    assert agent.workflow.lookup_keys == [
        agent.started.command.command_key,
        agent.started.command.command_key,
    ]


@pytest.mark.parametrize("malformation", ["absent", "wrong-key", "non-accepted"])
def test_malformed_accepted_dispatch_is_redacted_to_unknown(
    isolated_agent_database: IsolatedAgentDatabase,
    malformation: str,
) -> None:
    agent = runtime(isolated_agent_database)
    if malformation == "absent":
        receipt = None
    elif malformation == "wrong-key":
        receipt = WorkflowReceipt(
            command_key="wrong-command-key",
            outcome=WorkflowOutcome.ACCEPTED,
        )
    else:
        receipt = WorkflowReceipt.model_construct(
            command_key=agent.started.command.command_key,
            outcome=WorkflowOutcome.REJECTED,
        )
    malformed = WorkflowDispatchOutcome.model_construct(
        outcome=WorkflowOutcome.ACCEPTED,
        receipt=receipt,
        error_code=None,
    )
    scripted = ScriptedWorkflow(dispatch=malformed)
    dependencies = replace(agent.dependencies, workflow_orchestrator=scripted)

    result = dispatch_workflow_commands(None, limit=1, dependencies=dependencies)

    assert (result.dispatched, result.unknown) == (0, 1)
    state, attempts, persisted_receipt, error = command_state(agent)
    assert (state, attempts, persisted_receipt, error) == (
        WorkflowCommandState.UNKNOWN.value,
        1,
        None,
        "WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN",
    )


@pytest.mark.parametrize("malformation", ["absent", "wrong-key", "non-accepted"])
def test_malformed_confirmed_lookup_is_redacted_to_unknown(
    isolated_agent_database: IsolatedAgentDatabase,
    malformation: str,
) -> None:
    agent = runtime(isolated_agent_database)
    agent.workflow.fail_after_accept = True
    dispatch_workflow_commands(None, limit=1, dependencies=agent.dependencies)
    if malformation == "absent":
        receipt = None
    elif malformation == "wrong-key":
        receipt = WorkflowReceipt(
            command_key="wrong-command-key",
            outcome=WorkflowOutcome.ACCEPTED,
        )
    else:
        receipt = WorkflowReceipt.model_construct(
            command_key=agent.started.command.command_key,
            outcome=WorkflowOutcome.REJECTED,
        )
    malformed = WorkflowLookupResult.model_construct(
        outcome=WorkflowLookupOutcome.CONFIRMED,
        receipt=receipt,
        error_code=None,
    )
    scripted = ScriptedWorkflow(lookup=malformed)
    dependencies = replace(agent.dependencies, workflow_orchestrator=scripted)

    result = reconcile_workflow_commands(None, limit=1, dependencies=dependencies)

    assert (result.confirmed, result.still_unknown) == (0, 1)
    state, attempts, persisted_receipt, error = command_state(agent)
    assert (state, attempts, persisted_receipt, error) == (
        WorkflowCommandState.UNKNOWN.value,
        2,
        None,
        "WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN",
    )
    assert scripted.effect_requests == []
    assert scripted.lookup_keys == [agent.started.command.command_key]


def test_cancel_and_resume_route_only_the_exact_persisted_request(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    routes = RouteRecordingWorkflow()
    agent = runtime(isolated_agent_database)
    dependencies = replace(agent.dependencies, workflow_orchestrator=routes)
    dispatch_workflow_commands(None, limit=1, dependencies=dependencies)
    cancel = insert_command(
        agent,
        agent.started.command.model_copy(
            update={
                "id": agent.dependencies.new_id(),
                "command_key": f"cancel:{agent.started.attempt.id}:generation-7",
                "kind": WorkflowCommandKind.CANCEL,
                "generation": 7,
            }
        ),
    )
    resume = insert_command(
        agent,
        agent.started.command.model_copy(
            update={
                "id": agent.dependencies.new_id(),
                "command_key": f"resume:{agent.started.attempt.id}:generation-8",
                "kind": WorkflowCommandKind.RESUME,
                "generation": 8,
            }
        ),
    )

    result = dispatch_workflow_commands(None, limit=2, dependencies=dependencies)

    assert result.dispatched == 2
    assert [(request.command_key, request.generation) for request in routes.start_requests] == [
        (agent.started.command.command_key, agent.started.command.generation)
    ]
    assert [(request.command_key, request.generation) for request in routes.cancel_requests] == [
        (cancel.command_key, cancel.generation)
    ]
    assert [(request.command_key, request.generation) for request in routes.resume_requests] == [
        (resume.command_key, resume.generation)
    ]


def test_dev_adapter_serializes_concurrent_same_key_effect_and_lookup_records() -> None:
    adapter = DevTemporalAdapter()
    request = WorkflowCommandRequest(
        command_key="same-key",
        kind=WorkflowCommandKind.START,
        attempt_id="10000000-0000-0000-0000-000000009931",
        generation=4,
    )
    effect_barrier = Barrier(8)
    outcomes: list[WorkflowDispatchOutcome] = []
    outcome_lock = Lock()

    def call_effect() -> None:
        effect_barrier.wait()
        outcome = adapter.start(request)
        with outcome_lock:
            outcomes.append(outcome)

    effect_threads = [Thread(target=call_effect) for _ in range(8)]
    for thread in effect_threads:
        thread.start()
    for thread in effect_threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in effect_threads)
    assert len(outcomes) == 8
    assert all(outcome.outcome is WorkflowOutcome.ACCEPTED for outcome in outcomes)
    assert adapter.command_keys == [request.command_key]

    lookup_barrier = Barrier(8)
    lookups: list[WorkflowLookupResult] = []

    def call_lookup() -> None:
        lookup_barrier.wait()
        lookup = adapter.lookup(request.command_key)
        with outcome_lock:
            lookups.append(lookup)

    lookup_threads = [Thread(target=call_lookup) for _ in range(8)]
    for thread in lookup_threads:
        thread.start()
    for thread in lookup_threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in lookup_threads)
    assert len(lookups) == 8
    assert all(lookup.outcome is WorkflowLookupOutcome.CONFIRMED for lookup in lookups)
    assert adapter.lookup_keys == [request.command_key] * 8


def test_sensitive_adapter_exception_is_never_persisted_in_any_command_field(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    agent = runtime(isolated_agent_database)

    class SensitiveFailureWorkflow(DevTemporalAdapter):
        def start(self, _command: WorkflowCommandRequest) -> WorkflowDispatchOutcome:
            raise RuntimeError("SENSITIVE_EXCEPTION_SENTINEL")

    dependencies = replace(
        agent.dependencies,
        workflow_orchestrator=SensitiveFailureWorkflow(),
    )

    result = dispatch_workflow_commands(None, limit=1, dependencies=dependencies)

    assert result.unknown == 1
    record = command_record(agent)
    assert "SENSITIVE_EXCEPTION_SENTINEL" not in json.dumps(record, default=str)
    assert record["last_error_code"] == "WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN"
