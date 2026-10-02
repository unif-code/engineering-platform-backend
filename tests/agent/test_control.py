from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Barrier, Event, Lock, Thread
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import text

from control_plane.app.modules.agent import accept_workflow_event, cancel_attempt, resume_attempt
from control_plane.app.modules.agent.adapters.sqlalchemy import (
    SqlAlchemyAgentRepository,
    SqlAlchemyAgentUnitOfWork,
)
from control_plane.app.modules.agent.application.control import (
    AttemptControlResult,
    AttemptRevisionConflict,
    AttemptWaitingExpired,
    BindingDigestMismatch,
    CancelAttemptCommand,
    ResumeAttemptCommand,
)
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.events import (
    EventAcceptance,
    StaleRunnerGeneration,
)
from control_plane.app.modules.agent.application.runs import IdempotencyConflict, StartRunResult
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentAuditAppend,
    AgentRun,
    AttemptNotResumable,
    AttemptState,
    IllegalAttemptTransition,
)
from control_plane.app.modules.agent.ports import AgentUnitOfWork
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_events import (
    NOW,
    advance_to_running,
    dependencies,
    event,
    start,
    waiting_event,
)
from tests.agent.test_start_run import (
    AuditFailingTransactionRunner,
    PersistenceFailingTransactionRunner,
)


class ControllableClock:
    def __init__(self, now: datetime) -> None:
        self._now = now
        self._lock = Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._now

    def advance_to(self, now: datetime) -> None:
        with self._lock:
            self._now = now


class LockOrderRepository(SqlAlchemyAgentRepository):
    def __init__(
        self,
        delegate: SqlAlchemyAgentRepository,
        lock_order: list[str],
        *,
        run_locked: Event | None = None,
        release_run: Event | None = None,
    ) -> None:
        super().__init__(delegate.db)
        self._lock_order = lock_order
        self._run_locked = run_locked
        self._release_run = release_run

    def run_by_id(self, run_id: str, *, for_update: bool = False) -> AgentRun | None:
        result = super().run_by_id(run_id, for_update=for_update)
        if for_update:
            self._lock_order.append("run")
            if self._run_locked is not None:
                self._run_locked.set()
            if self._release_run is not None and not self._release_run.wait(timeout=5):
                raise TimeoutError("test did not release the Run row lock")
        return result

    def attempt_by_id(self, attempt_id: str, *, for_update: bool = False) -> AgentAttempt | None:
        result = super().attempt_by_id(attempt_id, for_update=for_update)
        if for_update:
            self._lock_order.append("attempt")
        return result


class LockOrderUnitOfWork:
    def __init__(
        self, delegate: SqlAlchemyAgentUnitOfWork, repository: LockOrderRepository
    ) -> None:
        self._delegate = delegate
        self._repository = repository

    def repository(self) -> LockOrderRepository:
        return self._repository

    def append_audit_event(self, event: AgentAuditAppend) -> None:
        self._delegate.append_audit_event(event)


class ObservedTransactionRunner:
    def __init__(
        self,
        database: IsolatedAgentDatabase,
        *,
        pause_after_run_lock: bool = False,
    ) -> None:
        self._engine = database.runtime
        self.backend_pid: int | None = None
        self.backend_pid_ready = Event()
        self.run_locked = Event()
        self.release_run = Event()
        self.lock_order: list[str] = []
        self._pause_after_run_lock = pause_after_run_lock

    def __call__[T](self, operation: Callable[[AgentUnitOfWork], T]) -> T:
        with self._engine.begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            self.backend_pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            self.backend_pid_ready.set()
            unit_of_work = SqlAlchemyAgentUnitOfWork(db)
            repository = LockOrderRepository(
                unit_of_work.repository(),
                self.lock_order,
                run_locked=self.run_locked if self._pause_after_run_lock else None,
                release_run=self.release_run if self._pause_after_run_lock else None,
            )
            return operation(LockOrderUnitOfWork(unit_of_work, repository))


def assert_postgres_blocked_by(
    database: IsolatedAgentDatabase,
    *,
    waiting_pid: int,
    blocking_pid: int,
) -> None:
    deadline = monotonic() + 5
    with database.owner.connect() as db:
        while monotonic() < deadline:
            blocked = db.execute(
                text(
                    "SELECT CAST(:blocking_pid AS INTEGER) = ANY("
                    "pg_blocking_pids(CAST(:waiting_pid AS INTEGER)))"
                ),
                {"blocking_pid": blocking_pid, "waiting_pid": waiting_pid},
            ).scalar_one()
            if blocked:
                return
    raise AssertionError(
        f"PostgreSQL backend {waiting_pid} was not blocked by backend {blocking_pid}"
    )


def persisted_control_state(
    database: IsolatedAgentDatabase, attempt_id: str
) -> dict[str, tuple[dict[str, object], ...]]:
    tables = (
        ("attempt", "agent.agent_attempt", "WHERE id=CAST(:attempt_id AS UUID)"),
        ("binding", "agent.execution_binding", "WHERE attempt_id=CAST(:attempt_id AS UUID)"),
        ("checkpoint", "agent.checkpoint", "WHERE attempt_id=CAST(:attempt_id AS UUID)"),
        ("workflow", "agent.workflow_command", "WHERE attempt_id=CAST(:attempt_id AS UUID)"),
        ("idempotency", "agent.idempotency_key", ""),
        ("audit", "audit.audit_event", ""),
    )
    with database.owner.connect() as db:
        return {
            name: tuple(
                dict(row)
                for row in db.execute(
                    text(f"SELECT * FROM {table} {where} ORDER BY id"),
                    {"attempt_id": attempt_id},
                ).mappings()
            )
            for name, table, where in tables
        }


def prepare_waiting(database: IsolatedAgentDatabase) -> tuple[AgentDependencies, StartRunResult]:
    deps = dependencies(database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)
    accept_workflow_event(None, event=waiting_event(started.attempt.id), dependencies=deps)
    return deps, started


def resume_command(
    started: StartRunResult, *, revision: int, key: str = "resume-901"
) -> ResumeAttemptCommand:
    return ResumeAttemptCommand(
        run_id=started.run.id,
        attempt_id=started.attempt.id,
        expected_revision=revision,
        actor="employee-901",
        idempotency_key=key,
        correlation_id="resume-correlation-901",
    )


def cancel_command(
    started: StartRunResult, *, revision: int, key: str = "cancel-901"
) -> CancelAttemptCommand:
    return CancelAttemptCommand(
        run_id=started.run.id,
        attempt_id=started.attempt.id,
        expected_revision=revision,
        actor="employee-901",
        idempotency_key=key,
        correlation_id="cancel-correlation-901",
    )


def prepare_finalizing(
    database: IsolatedAgentDatabase,
) -> tuple[AgentDependencies, StartRunResult, AgentAttempt]:
    deps = dependencies(database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)
    accepted = accept_workflow_event(
        None,
        event=event(
            started.attempt.id,
            event_id=str(uuid4()),
            event_type="ATTEMPT_FINALIZING",
            sequence=4,
            data={"evidenceRef": "artifact:already-recorded"},
        ),
        dependencies=deps,
    )
    return deps, started, accepted.attempt


@pytest.mark.parametrize("cancel_wins", [True, False])
@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED"])
def test_finalizing_cancel_and_completion_obey_the_first_locked_transition(
    isolated_agent_database: IsolatedAgentDatabase, cancel_wins: bool, terminal: str
) -> None:
    database = isolated_agent_database
    deps, started, finalizing = prepare_finalizing(database)
    before = persisted_control_state(database, finalizing.id)
    cancel_runner = ObservedTransactionRunner(database, pause_after_run_lock=cancel_wins)
    event_runner = ObservedTransactionRunner(database, pause_after_run_lock=not cancel_wins)
    cancel_results: list[AttemptControlResult] = []
    event_results: list[EventAcceptance] = []
    cancel_errors: list[BaseException] = []
    event_errors: list[BaseException] = []
    command = cancel_command(started, revision=finalizing.revision)

    def cancel() -> None:
        try:
            cancel_results.append(
                cancel_attempt(
                    None,
                    command=command,
                    dependencies=replace(deps, transaction_runner=cancel_runner),
                )
            )
        except BaseException as error:
            cancel_errors.append(error)

    def finish() -> None:
        try:
            event_results.append(
                accept_workflow_event(
                    None,
                    event=event(
                        finalizing.id,
                        event_id=str(uuid4()),
                        event_type=f"ATTEMPT_{terminal}",
                        sequence=5,
                    ),
                    dependencies=replace(deps, transaction_runner=event_runner),
                )
            )
        except BaseException as error:
            event_errors.append(error)

    cancel_thread, event_thread = Thread(target=cancel), Thread(target=finish)
    first, second = (cancel_thread, event_thread) if cancel_wins else (event_thread, cancel_thread)
    first_runner, second_runner = (
        (cancel_runner, event_runner) if cancel_wins else (event_runner, cancel_runner)
    )
    first.start()
    try:
        assert first_runner.run_locked.wait(timeout=5)
        second.start()
        assert second_runner.backend_pid_ready.wait(timeout=5)
        assert first_runner.backend_pid is not None and second_runner.backend_pid is not None
        assert_postgres_blocked_by(
            database, waiting_pid=second_runner.backend_pid, blocking_pid=first_runner.backend_pid
        )
    finally:
        first_runner.release_run.set()
        first.join(timeout=10)
        if second.ident is not None:
            second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert cancel_runner.lock_order == event_runner.lock_order == ["run", "attempt"]
    with database.runtime.connect() as db:
        repository = SqlAlchemyAgentRepository(db)
        current = repository.attempt_by_id(finalizing.id)
        run = repository.run_by_id(started.run.id)
    assert current is not None and run is not None
    assert current.revision == finalizing.revision + 1
    assert current.binding_id == finalizing.binding_id
    assert current.runner_generation == finalizing.runner_generation
    assert current.checkpoint == finalizing.checkpoint
    after = persisted_control_state(database, finalizing.id)
    assert after["binding"] == before["binding"]
    assert after["checkpoint"] == before["checkpoint"]
    cancel_commands = [row for row in after["workflow"] if row["kind"] == "CANCEL"]
    if cancel_wins:
        assert cancel_errors == [] and len(cancel_results) == 1
        assert len(event_errors) == 1 and isinstance(event_errors[0], IllegalAttemptTransition)
        assert current.state is AttemptState.CANCELING and run.state.value == "ACTIVE"
        assert current.event_sequence == finalizing.event_sequence
        assert len(cancel_commands) == 1
        assert cancel_attempt(None, command=command, dependencies=deps) == cancel_results[0]
        assert persisted_control_state(database, finalizing.id) == after
        # Rejected completion does not consume an accepted-event sequence.
        accept_workflow_event(
            None,
            event=event(
                finalizing.id, event_id=str(uuid4()), event_type="ATTEMPT_CANCELED", sequence=5
            ),
            dependencies=deps,
        )
    else:
        assert event_errors == [] and len(event_results) == 1
        assert len(cancel_errors) == 1 and isinstance(cancel_errors[0], AttemptRevisionConflict)
        assert current.state.value == run.state.value == terminal
        assert cancel_commands == []
    with database.owner.connect() as db:
        evidence = db.execute(
            text(
                "SELECT data FROM agent.canonical_event WHERE attempt_id=CAST(:id AS UUID) "
                "AND event_type='ATTEMPT_FINALIZING'"
            ),
            {"id": finalizing.id},
        ).scalar_one()
    assert evidence == {"evidenceRef": "artifact:already-recorded"}


@pytest.mark.parametrize(
    "runner_type", [AuditFailingTransactionRunner, PersistenceFailingTransactionRunner]
)
def test_finalizing_cancel_failure_rolls_back_state_command_audit_and_receipt(
    isolated_agent_database: IsolatedAgentDatabase,
    runner_type: type[AuditFailingTransactionRunner] | type[PersistenceFailingTransactionRunner],
) -> None:
    deps, started, finalizing = prepare_finalizing(isolated_agent_database)
    before = persisted_control_state(isolated_agent_database, finalizing.id)
    with pytest.raises(RuntimeError, match="unavailable"):
        cancel_attempt(
            None,
            command=cancel_command(started, revision=finalizing.revision),
            dependencies=replace(deps, transaction_runner=runner_type(isolated_agent_database)),
        )
    assert persisted_control_state(isolated_agent_database, finalizing.id) == before


def test_resume_rotates_generation_and_rejects_late_event(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started = prepare_waiting(isolated_agent_database)
    resumed = resume_attempt(
        None,
        command=resume_command(started, revision=6),
        dependencies=deps,
    )

    assert resumed.attempt.runner_generation == 2
    assert resumed.attempt.binding_id == started.attempt.binding_id
    assert resumed.attempt.event_sequence == 0
    with pytest.raises(StaleRunnerGeneration):
        accept_workflow_event(
            None,
            event=event(
                started.attempt.id,
                event_id="10000000-0000-0000-0000-000000001310",
                event_type="ATTEMPT_FINALIZING",
                generation=1,
                sequence=5,
            ),
            dependencies=deps,
        )
    assert resumed.attempt.state is AttemptState.QUEUED


def test_resume_rejects_expired_wait_without_a_command(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started = prepare_waiting(isolated_agent_database)
    expired = replace(deps, clock=lambda: NOW + timedelta(hours=2))

    with pytest.raises(AttemptWaitingExpired):
        resume_attempt(
            None,
            command=resume_command(started, revision=6),
            dependencies=expired,
        )
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.workflow_command")).scalar_one() == 1


def test_resume_rejects_when_waiting_deadline_equals_locked_clock_sample(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started = prepare_waiting(isolated_agent_database)
    deadline = NOW + timedelta(hours=1)
    before = persisted_control_state(isolated_agent_database, started.attempt.id)

    with pytest.raises(AttemptWaitingExpired):
        resume_attempt(
            None,
            command=resume_command(started, revision=6),
            dependencies=replace(deps, clock=lambda: deadline),
        )

    assert persisted_control_state(isolated_agent_database, started.attempt.id) == before


def test_resume_samples_clock_after_waiting_for_run_and_attempt_locks(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started = prepare_waiting(isolated_agent_database)
    before = persisted_control_state(isolated_agent_database, started.attempt.id)
    clock = ControllableClock(NOW + timedelta(minutes=59))
    runner = ObservedTransactionRunner(isolated_agent_database)
    blocked_deps = replace(deps, transaction_runner=runner, clock=clock)
    errors: list[BaseException] = []

    with isolated_agent_database.owner.connect() as blocker:
        transaction = blocker.begin()
        blocker_pid = blocker.execute(text("SELECT pg_backend_pid()")).scalar_one()
        blocker.execute(
            text("SELECT id FROM agent.agent_attempt WHERE id=CAST(:id AS UUID) FOR UPDATE"),
            {"id": started.attempt.id},
        )

        def invoke_resume() -> None:
            try:
                resume_attempt(
                    None,
                    command=resume_command(started, revision=6),
                    dependencies=blocked_deps,
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        worker = Thread(target=invoke_resume)
        worker.start()
        try:
            assert runner.backend_pid_ready.wait(timeout=5)
            assert runner.backend_pid is not None
            assert_postgres_blocked_by(
                isolated_agent_database,
                waiting_pid=runner.backend_pid,
                blocking_pid=blocker_pid,
            )
            clock.advance_to(NOW + timedelta(hours=1, microseconds=1))
        finally:
            transaction.commit()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], AttemptWaitingExpired)
    assert runner.lock_order == ["run", "attempt"]
    assert persisted_control_state(isolated_agent_database, started.attempt.id) == before


def test_terminal_cancel_is_safe_and_does_not_queue_another_command(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    canceled = cancel_attempt(
        None,
        command=cancel_command(started, revision=3),
        dependencies=deps,
    )
    replay = cancel_attempt(
        None,
        command=cancel_command(started, revision=3),
        dependencies=deps,
    )

    assert canceled.attempt.state is AttemptState.CANCELING
    assert replay == canceled
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.workflow_command")).scalar_one() == 2


def test_competing_cancel_and_resume_have_one_revision_winner(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started = prepare_waiting(isolated_agent_database)
    barrier = Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def invoke(command: CancelAttemptCommand | ResumeAttemptCommand) -> None:
        try:
            barrier.wait(timeout=5)
            if isinstance(command, CancelAttemptCommand):
                results.append(cancel_attempt(None, command=command, dependencies=deps))
            else:
                results.append(resume_attempt(None, command=command, dependencies=deps))
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    cancel = Thread(target=invoke, args=(cancel_command(started, revision=6),))
    resume = Thread(target=invoke, args=(resume_command(started, revision=6),))
    cancel.start()
    resume.start()
    cancel.join(timeout=10)
    resume.join(timeout=10)

    assert not cancel.is_alive()
    assert not resume.is_alive()
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], AttemptRevisionConflict)


@pytest.mark.parametrize("control_kind", ["cancel", "resume"])
def test_terminal_event_and_control_share_run_attempt_lock_order_without_deadlock(
    isolated_agent_database: IsolatedAgentDatabase,
    control_kind: str,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    canceling = cancel_attempt(
        None,
        command=cancel_command(started, revision=3, key="prepare-terminal-race"),
        dependencies=deps,
    )
    terminal = event(
        started.attempt.id,
        event_id="10000000-0000-0000-0000-000000001390",
        event_type="ATTEMPT_CANCELED",
        sequence=2,
    )
    event_runner = ObservedTransactionRunner(isolated_agent_database, pause_after_run_lock=True)
    control_runner = ObservedTransactionRunner(isolated_agent_database)
    event_deps = replace(deps, transaction_runner=event_runner)
    control_deps = replace(deps, transaction_runner=control_runner)
    terminal_results: list[EventAcceptance] = []
    terminal_errors: list[BaseException] = []
    control_errors: list[BaseException] = []

    def deliver_terminal() -> None:
        try:
            terminal_results.append(
                accept_workflow_event(None, event=terminal, dependencies=event_deps)
            )
        except BaseException as error:  # pragma: no cover - asserted below
            terminal_errors.append(error)

    def invoke_control() -> None:
        try:
            if control_kind == "cancel":
                cancel_attempt(
                    None,
                    command=cancel_command(
                        started,
                        revision=canceling.attempt.revision,
                        key="terminal-race-cancel",
                    ),
                    dependencies=control_deps,
                )
            else:
                resume_attempt(
                    None,
                    command=resume_command(
                        started,
                        revision=canceling.attempt.revision,
                        key="terminal-race-resume",
                    ),
                    dependencies=control_deps,
                )
        except BaseException as error:  # pragma: no cover - asserted below
            control_errors.append(error)

    terminal_worker = Thread(target=deliver_terminal)
    control_worker = Thread(target=invoke_control)
    terminal_worker.start()
    assert event_runner.run_locked.wait(timeout=5)
    control_worker.start()
    try:
        assert event_runner.backend_pid is not None
        assert control_runner.backend_pid_ready.wait(timeout=5)
        assert control_runner.backend_pid is not None
        assert_postgres_blocked_by(
            isolated_agent_database,
            waiting_pid=control_runner.backend_pid,
            blocking_pid=event_runner.backend_pid,
        )
    finally:
        event_runner.release_run.set()
    terminal_worker.join(timeout=10)
    control_worker.join(timeout=10)

    assert not terminal_worker.is_alive()
    assert not control_worker.is_alive()
    assert terminal_errors == []
    assert len(terminal_results) == 1
    assert terminal_results[0].attempt.state is AttemptState.CANCELED
    assert len(control_errors) == 1
    assert isinstance(control_errors[0], AttemptRevisionConflict)
    assert event_runner.lock_order == ["run", "attempt"]
    assert control_runner.lock_order == ["run", "attempt"]
    with isolated_agent_database.owner.connect() as db:
        attempt_and_run = db.execute(
            text(
                "SELECT attempt.state AS attempt_state, run.state AS run_state "
                "FROM agent.agent_attempt AS attempt "
                "JOIN agent.agent_run AS run ON run.id=attempt.run_id "
                "WHERE attempt.id=CAST(:id AS UUID)"
            ),
            {"id": started.attempt.id},
        ).one()
        assert attempt_and_run == ("CANCELED", "CANCELED")


def test_terminal_attempt_cannot_resume(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    canceling = cancel_attempt(None, command=cancel_command(started, revision=3), dependencies=deps)
    accept_workflow_event(
        None,
        event=event(
            started.attempt.id,
            event_id="10000000-0000-0000-0000-000000001311",
            event_type="ATTEMPT_CANCELED",
            sequence=2,
        ),
        dependencies=deps,
    )

    with pytest.raises(AttemptNotResumable):
        resume_attempt(
            None,
            command=resume_command(started, revision=canceling.attempt.revision + 1),
            dependencies=deps,
        )


def test_terminal_cancel_is_safe_without_another_workflow_command(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    canceling = cancel_attempt(None, command=cancel_command(started, revision=3), dependencies=deps)
    accept_workflow_event(
        None,
        event=event(
            started.attempt.id,
            event_id="10000000-0000-0000-0000-000000001312",
            event_type="ATTEMPT_CANCELED",
            sequence=2,
        ),
        dependencies=deps,
    )

    terminal = cancel_attempt(
        None,
        command=cancel_command(
            started, revision=canceling.attempt.revision + 1, key="cancel-terminal-901"
        ),
        dependencies=deps,
    )

    assert terminal.attempt.state is AttemptState.CANCELED
    assert terminal.command is None
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.workflow_command")).scalar_one() == 2


def test_resume_rejects_binding_digest_mismatch_without_a_command(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started = prepare_waiting(isolated_agent_database)
    with isolated_agent_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE agent.execution_binding SET digest=:digest "
                "WHERE attempt_id=CAST(:id AS UUID)"
            ),
            {"id": started.attempt.id, "digest": "0" * 64},
        )

    with pytest.raises(BindingDigestMismatch):
        resume_attempt(None, command=resume_command(started, revision=6), dependencies=deps)
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.workflow_command")).scalar_one() == 1
        assert db.execute(text("SELECT count(*) FROM agent.idempotency_key")).scalar_one() == 1


def test_changed_resume_body_with_same_key_conflicts_without_second_command(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started = prepare_waiting(isolated_agent_database)
    result = resume_attempt(None, command=resume_command(started, revision=6), dependencies=deps)

    with pytest.raises(IdempotencyConflict):
        resume_attempt(
            None,
            command=resume_command(started, revision=7),
            dependencies=deps,
        )
    assert result.attempt.runner_generation == 2
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.workflow_command")).scalar_one() == 2


def test_event_cancel_and_resume_audits_exclude_sensitive_sentinel(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    sentinel = "secret-event-control-sentinel"
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    accept_workflow_event(
        None,
        event=event(
            started.attempt.id,
            event_id="10000000-0000-0000-0000-000000001370",
            event_type="ATTEMPT_PROVISIONING",
            sequence=2,
        ).model_copy(update={"summary": sentinel, "correlation_id": sentinel}),
        dependencies=deps,
    )
    cancel_attempt(
        None,
        command=cancel_command(started, revision=4, key=sentinel).model_copy(
            update={"correlation_id": sentinel}
        ),
        dependencies=deps,
    )

    waiting = start(deps, idempotency_key="audit-resume-start")
    advance_to_running(deps, waiting.attempt.id)
    accept_workflow_event(None, event=waiting_event(waiting.attempt.id), dependencies=deps)
    resume_attempt(
        None,
        command=resume_command(waiting, revision=6, key=f"{sentinel}-resume").model_copy(
            update={"correlation_id": sentinel}
        ),
        dependencies=deps,
    )
    with isolated_agent_database.owner.connect() as db:
        rows = db.execute(
            text(
                "SELECT id, occurred_at, actor, actor_type, action, target_type, target_id, "
                "result, reason, correlation_id, request_id, schema_version "
                "FROM audit.audit_event WHERE action IN "
                "('agent.event.accept', 'agent.attempt.cancel', 'agent.attempt.resume')"
            )
        ).mappings()
        assert all(sentinel not in str(value) for row in rows for value in row.values())
