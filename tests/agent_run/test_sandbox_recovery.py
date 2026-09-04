from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from sqlalchemy import text

from control_plane.app.modules.agent_run import (
    CancelExecutionCommand,
    CancellationReason,
    CheckpointAndReleaseCommand,
    FinalizeExecutionCommand,
    MaterializationFailed,
    MaterializationReady,
    MaterializationState,
    ReconcileLeaseCommand,
)
from control_plane.app.modules.agent_run.adapters import (
    RestrictedDevSandboxAdapter,
    SqlAlchemySandboxRepository,
)
from control_plane.app.modules.agent_run.ports.runtime import (
    RuntimeMaterializationError,
    RuntimeMaterializationRequest,
    RuntimeObservation,
    RuntimePresence,
    RuntimeReadiness,
)
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import _command, _controller
from tests.agent_run.test_generation_fencing import _context, _guard


class _CrashAfterProvision:
    def __init__(self, delegate: RestrictedDevSandboxAdapter) -> None:
        self._delegate = delegate

    def provision(self, request: RuntimeMaterializationRequest) -> RuntimeReadiness:
        self._delegate.provision(request)
        raise SystemExit("simulated process loss after runtime provision")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _CrashAfterFence:
    def __init__(self, delegate: RestrictedDevSandboxAdapter) -> None:
        self._delegate = delegate

    def fence(self, guard: Any) -> None:
        self._delegate.fence(guard)
        raise SystemExit("simulated process loss after runtime fence")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _CrashBeforeProvision:
    def __init__(self, delegate: RestrictedDevSandboxAdapter) -> None:
        self._delegate = delegate

    def provision(self, request: RuntimeMaterializationRequest) -> RuntimeReadiness:
        del request
        raise SystemExit("simulated process loss before runtime provision")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _durable_runtime(
    database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> RestrictedDevSandboxAdapter:
    repository = tmp_path / "repository-1"
    repository.mkdir(exist_ok=True)
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
        state_engine=database.runtime,
    )


def test_reservation_after_commit_crash_is_taken_over_with_encrypted_recovery(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    command = _command(now, key="sandbox-durable-provision-recovery")
    first_runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    first_controller = _controller(
        isolated_agent_run_database.runtime,
        _CrashAfterProvision(first_runtime),
        now,
    )

    with pytest.raises(SystemExit, match="process loss"):
        first_controller.provision_materialization(command)

    with isolated_agent_run_database.owner.connect() as db:
        before = db.execute(
            text(
                "SELECT m.state, m.recovery_capsule, r.state, r.phase, r.subject_id "
                "FROM agent_run.sandbox_materialization m "
                "JOIN agent_run.command_receipt r ON r.subject_id=m.id"
            )
        ).one()
    assert before[0] == "PROVISIONING"
    assert before[1] is not None
    assert b"local-test-fence" not in before[1]
    assert before[2:] == ("IN_PROGRESS", "RESERVED", before[4])

    restarted_runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    restarted_controller = _controller(
        isolated_agent_run_database.runtime,
        restarted_runtime,
        now + timedelta(seconds=31),
    )
    recovered = restarted_controller.provision_materialization(command)

    assert isinstance(recovered, MaterializationReady)
    assert recovered.handle.generation == 1
    assert restarted_runtime.events == ()
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text("SELECT state, revision FROM agent_run.sandbox_materialization")
        ).one() == ("READY", 2)
        assert db.execute(text("SELECT state, phase FROM agent_run.command_receipt")).one() == (
            "COMPLETED",
            "COMPLETED",
        )
        assert (
            db.execute(text("SELECT count(*) FROM agent_run.runner_generation")).scalar_one() == 1
        )


def test_cleanup_after_external_step_crash_resumes_without_repeating_the_step(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    provision_command = _command(now, key="sandbox-cleanup-crash-provision")
    runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    provision_controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    ready = provision_controller.provision_materialization(provision_command)
    assert isinstance(ready, MaterializationReady)
    cancel = CancelExecutionCommand(
        context=_context("sandbox-cleanup-crash-cancel"),
        execution=provision_command.binding.execution,
        reason=CancellationReason.SECURITY_VIOLATION,
    )
    crashing_controller = _controller(
        isolated_agent_run_database.runtime,
        _CrashAfterFence(runtime),
        now,
    )

    with pytest.raises(SystemExit, match="runtime fence"):
        crashing_controller.cancel_execution(cancel)

    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT m.state, m.fenced_at, s.side_effects_fenced "
                "FROM agent_run.sandbox_materialization m "
                "JOIN agent_run.runtime_state s ON s.materialization_id=m.id"
            )
        ).one() == ("CANCELING", None, True)

    restarted_runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    restarted_controller = _controller(
        isolated_agent_run_database.runtime,
        restarted_runtime,
        now + timedelta(seconds=31),
    )
    recovered = restarted_controller.cancel_execution(cancel)

    assert recovered.state is MaterializationState.CANCELED
    assert [event.action for event in restarted_runtime.events] == [
        "revoke_secret",
        "destroy",
    ]
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT state, cancellation_reason, fenced_at IS NOT NULL, "
                "secret_revoked_at IS NOT NULL, lease_released_at IS NOT NULL, "
                "destroyed_at IS NOT NULL FROM agent_run.sandbox_materialization"
            )
        ).one() == ("CANCELED", "SECURITY_VIOLATION", True, True, True, True)
        assert db.execute(
            text("SELECT active_attempts, active_units FROM agent_run.capacity_ledger")
        ).one() == (0, 0)
        terminal_audit = db.execute(
            text(
                "SELECT result, reason FROM audit.audit_event "
                "WHERE action='sandbox.execution.cancel' AND result='SUCCEEDED'"
            )
        ).one()
    assert terminal_audit == ("SUCCEEDED", "SECURITY_VIOLATION")


def test_orphaned_provisioning_with_confirmed_absence_is_reconciled_failed(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    provision = _command(now, key="sandbox-orphaned-provisioning")
    first_runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    first_controller = _controller(
        isolated_agent_run_database.runtime,
        _CrashBeforeProvision(first_runtime),
        now,
    )
    with pytest.raises(SystemExit, match="before runtime provision"):
        first_controller.provision_materialization(provision)

    restarted_runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    restarted_controller = _controller(
        isolated_agent_run_database.runtime,
        restarted_runtime,
        now + timedelta(seconds=31),
    )
    receipt = restarted_controller.reconcile_lease(
        ReconcileLeaseCommand(
            context=_context("sandbox-orphaned-provisioning-reconcile"),
            environment_id=provision.binding.environment.environment_id,
            execution_id=provision.binding.execution.execution_id,
            observed_at=now + timedelta(seconds=31),
        )
    )

    assert receipt.reconciled_count == 1
    assert receipt.items[0].state is MaterializationState.FAILED
    assert restarted_runtime.events == ()
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT state, evidence_persisted_at IS NOT NULL, fenced_at IS NOT NULL, "
                "secret_revoked_at IS NOT NULL, lease_released_at IS NOT NULL, "
                "destroyed_at IS NOT NULL FROM agent_run.sandbox_materialization"
            )
        ).one() == ("FAILED", True, True, True, True, True)
        assert db.execute(
            text("SELECT active_attempts, active_units FROM agent_run.capacity_ledger")
        ).one() == (0, 0)


def test_unknown_runtime_observation_quarantines_without_guessing_cleanup_success(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    provision = _command(now, key="sandbox-unknown-observation-provision")
    initial_runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    ready = _controller(
        isolated_agent_run_database.runtime,
        initial_runtime,
        now,
    ).provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)
    unavailable_runtime = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": tmp_path / "repository-1"},
        state_engine=isolated_agent_run_database.runtime,
        observation_available=False,
    )
    cancel = CancelExecutionCommand(
        context=_context("sandbox-unknown-observation-cancel"),
        execution=provision.binding.execution,
        reason=CancellationReason.CANCELED,
    )

    quarantined = _controller(
        isolated_agent_run_database.runtime,
        unavailable_runtime,
        now,
    ).cancel_execution(cancel)

    assert quarantined.state is MaterializationState.QUARANTINED
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT evidence_persisted_at, fenced_at, secret_revoked_at, "
                "lease_released_at, destroyed_at FROM agent_run.sandbox_materialization"
            )
        ).one() == (None, None, None, None, None)
        assert (
            db.execute(text("SELECT state FROM agent_run.capacity_lease")).scalar_one() == "ACTIVE"
        )

    recovered_runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    recovered = _controller(
        isolated_agent_run_database.runtime,
        recovered_runtime,
        now + timedelta(seconds=31),
    ).cancel_execution(cancel)

    assert recovered.state is MaterializationState.CANCELED
    assert [event.action for event in recovered_runtime.events] == [
        "evidence",
        "fence",
        "revoke_secret",
        "destroy",
    ]


def test_failed_provision_with_unknown_observation_resumes_the_same_cleanup_command(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    _durable_runtime(isolated_agent_run_database, tmp_path)
    runtime = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": tmp_path / "repository-1"},
        state_engine=isolated_agent_run_database.runtime,
        fail_steps=frozenset({"provision"}),
        observation_available=False,
    )
    command = _command(now, key="sandbox-failed-provision-unknown")
    failed = _controller(
        isolated_agent_run_database.runtime, runtime, now
    ).provision_materialization(command)
    assert isinstance(failed, MaterializationFailed)
    assert failed.denial.retryable is True
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT state FROM agent_run.sandbox_materialization")).scalar_one()
            == "QUARANTINED"
        )
        assert (
            db.execute(text("SELECT active_attempts FROM agent_run.capacity_ledger")).scalar_one()
            == 1
        )

    recovered_runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    recovered = _controller(
        isolated_agent_run_database.runtime,
        recovered_runtime,
        now + timedelta(seconds=31),
    ).provision_materialization(command)
    assert isinstance(recovered, MaterializationFailed)
    assert recovered_runtime.events == ()
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT state FROM agent_run.sandbox_materialization")).scalar_one()
            == "FAILED"
        )
        assert (
            db.execute(text("SELECT active_attempts FROM agent_run.capacity_ledger")).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE action LIKE 'sandbox.cleanup.%'")
            ).scalar_one()
            == 5
        )


class _CrashBeforeCleanupIntent(SqlAlchemySandboxRepository):
    def begin_cleanup(self, *args: Any, **kwargs: Any) -> int:
        raise SystemExit("simulated process loss before cleanup intent")


def test_finalize_takeover_after_claim_before_intent_resumes_same_key(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    ready = _controller(
        isolated_agent_run_database.runtime, runtime, now
    ).provision_materialization(_command(now, key="sandbox-claim-crash-provision"))
    assert isinstance(ready, MaterializationReady)
    command = FinalizeExecutionCommand(
        context=_context("sandbox-claim-crash-finalize"),
        guard=_guard(ready),
        evidence_refs=(),
    )
    with pytest.raises(SystemExit, match="before cleanup intent"):
        _controller(
            isolated_agent_run_database.runtime,
            runtime,
            now,
            repository_factory=_CrashBeforeCleanupIntent,
        ).finalize_execution(command)
    recovered = _controller(
        isolated_agent_run_database.runtime,
        runtime,
        now + timedelta(seconds=31),
    ).finalize_execution(command)
    assert recovered.state is MaterializationState.FINALIZED
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT active_attempts FROM agent_run.capacity_ledger")).scalar_one()
            == 0
        )


class _CrashBeforeReconciliationReceipt(SqlAlchemySandboxRepository):
    def record_reconciliation(self, **values: Any) -> bool:
        raise SystemExit("simulated process loss before reconciliation receipt")


def test_reconcile_takeover_preserves_committed_items_after_terminal_before_receipt_crash(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    provision = _command(now, key="sandbox-reconcile-receipt-provision")
    runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    _controller(isolated_agent_run_database.runtime, runtime, now).provision_materialization(
        provision
    )
    expired = now + timedelta(hours=1)
    command = ReconcileLeaseCommand(
        context=_context("sandbox-reconcile-receipt-crash"),
        environment_id=provision.binding.environment.environment_id,
        observed_at=expired,
    )
    with pytest.raises(SystemExit, match="before reconciliation receipt"):
        _controller(
            isolated_agent_run_database.runtime,
            runtime,
            expired,
            repository_factory=_CrashBeforeReconciliationReceipt,
        ).reconcile_lease(command)
    recovered = _controller(
        isolated_agent_run_database.runtime,
        _durable_runtime(isolated_agent_run_database, tmp_path),
        expired + timedelta(seconds=31),
    ).reconcile_lease(command)
    assert recovered.reconciled_count == 1
    assert len(recovered.items) == 1
    assert recovered.items[0].state is MaterializationState.TIMED_OUT
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='sandbox.cleanup.destroyed'"
                )
            ).scalar_one()
            == 1
        )


class _BlockedProvision:
    def __init__(self, delegate: RestrictedDevSandboxAdapter) -> None:
        self.delegate = delegate
        self.called = Event()
        self.release = Event()

    def provision(self, request: RuntimeMaterializationRequest) -> RuntimeReadiness:
        self.called.set()
        assert self.release.wait(timeout=15)
        return self.delegate.provision(request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


def test_late_provision_cannot_reactivate_after_absence_reconciliation_fences_it(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    blocked = _BlockedProvision(runtime)
    command = _command(now, key="sandbox-late-provision-fence")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _controller(
                isolated_agent_run_database.runtime, blocked, now
            ).provision_materialization,
            command,
        )
        assert blocked.called.wait(timeout=15)
        try:
            reconciled = _controller(
                isolated_agent_run_database.runtime,
                _durable_runtime(isolated_agent_run_database, tmp_path),
                now + timedelta(seconds=31),
            ).reconcile_lease(
                ReconcileLeaseCommand(
                    context=_context("sandbox-late-provision-reconcile"),
                    environment_id=command.binding.environment.environment_id,
                    observed_at=now + timedelta(seconds=31),
                )
            )
            assert reconciled.reconciled_count == 1
        finally:
            blocked.release.set()
        result = future.result(timeout=15)
    assert isinstance(result, MaterializationFailed)
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent_run.runtime_state")).scalar_one() == 0
        assert (
            db.execute(text("SELECT active_attempts FROM agent_run.capacity_ledger")).scalar_one()
            == 0
        )


class _ObservationOutage:
    def __init__(self, delegate: RestrictedDevSandboxAdapter, mode: str) -> None:
        self._delegate = delegate
        self._mode = mode
        self.observation_calls = 0

    def observe(self, materialization_id: str) -> RuntimeObservation:
        self.observation_calls += 1
        if self._mode == "unavailable":
            raise RuntimeMaterializationError("simulated observation outage")
        return RuntimeObservation(
            materialization_id=materialization_id, presence=RuntimePresence.UNKNOWN
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


@pytest.mark.parametrize("observation", ["available", "unknown", "unavailable"])
@pytest.mark.parametrize("operation", ["finalize_execution", "checkpoint_and_release"])
def test_cleanup_retry_finishes_original_receipt_after_reconciliation_completes_it(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
    operation: str,
    observation: str,
) -> None:
    now = datetime.now(UTC)
    runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    provision = _command(now, key="sandbox-cleanup-reconcile-provision")
    ready = _controller(
        isolated_agent_run_database.runtime, runtime, now
    ).provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)
    failing = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": tmp_path / "repository-1"},
        state_engine=isolated_agent_run_database.runtime,
        fail_steps=frozenset({"revoke_secret"}),
    )
    command_type = (
        FinalizeExecutionCommand
        if operation == "finalize_execution"
        else CheckpointAndReleaseCommand
    )
    command = command_type(
        context=_context("sandbox-cleanup-reconcile-original"),
        guard=_guard(ready),
        evidence_refs=(),
    )
    quarantined = getattr(
        _controller(isolated_agent_run_database.runtime, failing, now), operation
    )(command)
    assert quarantined.state is MaterializationState.QUARANTINED
    later = now + timedelta(seconds=31)
    controller = _controller(isolated_agent_run_database.runtime, runtime, later)
    reconciled = controller.reconcile_lease(
        ReconcileLeaseCommand(
            context=_context("sandbox-cleanup-reconcile-resume"),
            environment_id=ready.handle.environment_id,
            observed_at=later,
        )
    )
    assert reconciled.reconciled_count == 1
    with isolated_agent_run_database.owner.connect() as db:
        before = db.execute(text("SELECT * FROM agent_run.sandbox_materialization")).one()
        step_audit_before = db.execute(
            text(
                "SELECT * FROM audit.audit_event WHERE action LIKE 'sandbox.cleanup.%' ORDER BY id"
            )
        ).all()
        assert (
            db.execute(
                text("SELECT state FROM agent_run.command_receipt WHERE idempotency_key=:key"),
                {"key": command.context.idempotency_key},
            ).scalar_one()
            == "IN_PROGRESS"
        )
    previous_events = runtime.events
    outage = _ObservationOutage(runtime, observation)
    if observation != "available":
        controller = _controller(isolated_agent_run_database.runtime, outage, later)
    replayed = getattr(controller, operation)(command)
    assert replayed.state is (
        MaterializationState.FINALIZED
        if operation == "finalize_execution"
        else MaterializationState.RELEASED
    )
    assert getattr(controller, operation)(command) == replayed
    assert outage.observation_calls == 0
    assert runtime.events == previous_events
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(text("SELECT * FROM agent_run.sandbox_materialization")).one() == before
        assert (
            db.execute(
                text(
                    "SELECT * FROM audit.audit_event "
                    "WHERE action LIKE 'sandbox.cleanup.%' ORDER BY id"
                )
            ).all()
            == step_audit_before
        )
        assert db.execute(
            text("SELECT active_attempts, active_units FROM agent_run.capacity_ledger")
        ).one() == (0, 0)
        assert db.execute(
            text(
                "SELECT state, owner_id, phase FROM agent_run.command_receipt "
                "WHERE idempotency_key=:key"
            ),
            {"key": command.context.idempotency_key},
        ).one() == ("COMPLETED", None, "COMPLETED")
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='sandbox.cleanup.destroyed'"
                )
            ).scalar_one()
            == 1
        )
