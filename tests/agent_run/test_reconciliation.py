from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from control_plane.app.modules.agent_run import (
    CancelExecutionCommand,
    CancellationReason,
    DenialCode,
    MaterializationReady,
    MaterializationState,
    ReconcileLeaseCommand,
)
from control_plane.app.modules.agent_run.adapters import RestrictedDevSandboxAdapter
from control_plane.app.modules.agent_run.application.errors import SandboxApplicationError
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import MutableClock, _command, _controller
from tests.agent_run.test_generation_fencing import _context


def _failing_runtime(tmp_path: Path) -> RestrictedDevSandboxAdapter:
    repository = tmp_path / "repository-1"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
        fail_steps=frozenset({"revoke_secret"}),
    )


def test_partial_cleanup_is_quarantined_then_reconcile_resumes_without_reactivation(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _failing_runtime(tmp_path)
    clock = MutableClock(now)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now, clock=clock)
    provision = _command(now, key="sandbox-quarantine-provision")
    ready = controller.provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)

    quarantined = controller.cancel_execution(
        CancelExecutionCommand(
            context=_context("sandbox-quarantine-cancel"),
            execution=provision.binding.execution,
            reason=CancellationReason.CANCELED,
        )
    )

    assert quarantined.state is MaterializationState.QUARANTINED
    assert quarantined.denial is not None
    assert quarantined.denial.code is DenialCode.RESOURCE_EXHAUSTED
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT state, fenced_at IS NOT NULL, secret_revoked_at IS NULL, "
                "lease_released_at IS NULL, destroyed_at IS NULL, "
                "cleanup_terminal_state FROM agent_run.sandbox_materialization"
            )
        ).one() == ("QUARANTINED", True, True, True, True, "CANCELED")
        assert (
            db.execute(text("SELECT state FROM agent_run.capacity_lease")).scalar_one() == "ACTIVE"
        )
        committed_step_audits = db.execute(
            text(
                "SELECT action, count(*) FROM audit.audit_event "
                "WHERE action LIKE 'sandbox.cleanup.%' GROUP BY action ORDER BY action"
            )
        ).all()
    assert [tuple(row) for row in committed_step_audits] == [
        ("sandbox.cleanup.evidence_persisted", 1),
        ("sandbox.cleanup.fenced", 1),
    ]

    runtime.clear_failures()
    clock.advance(timedelta(hours=1))
    reconciliation = controller.reconcile_lease(
        ReconcileLeaseCommand(
            context=_context("sandbox-quarantine-reconcile"),
            environment_id=provision.binding.environment.environment_id,
            execution_id=provision.binding.execution.execution_id,
            observed_at=now + timedelta(hours=1),
        )
    )

    assert reconciliation.reconciled_count == 1
    assert reconciliation.items[0].state is MaterializationState.CANCELED
    assert [event.action for event in runtime.events] == [
        "provision",
        "evidence",
        "fence",
        "revoke_secret",
        "destroy",
    ]
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT state, secret_revoked_at IS NOT NULL, "
                "lease_released_at IS NOT NULL, destroyed_at IS NOT NULL "
                "FROM agent_run.sandbox_materialization"
            )
        ).one() == ("CANCELED", True, True, True)
        assert db.execute(
            text("SELECT active_attempts, active_units FROM agent_run.capacity_ledger")
        ).one() == (0, 0)
        all_step_audits = db.execute(
            text(
                "SELECT action, count(*) FROM audit.audit_event "
                "WHERE action LIKE 'sandbox.cleanup.%' GROUP BY action ORDER BY action"
            )
        ).all()
    assert [tuple(row) for row in all_step_audits] == [
        ("sandbox.cleanup.capacity_released", 1),
        ("sandbox.cleanup.destroyed", 1),
        ("sandbox.cleanup.evidence_persisted", 1),
        ("sandbox.cleanup.fenced", 1),
        ("sandbox.cleanup.secret_revoked", 1),
    ]


def test_expired_active_lease_is_timed_out_and_empty_scope_does_not_guess(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    repository = tmp_path / "repository-1"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runtime = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
    )
    clock = MutableClock(now)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now, clock=clock)
    provision = _command(now, key="sandbox-expiry-provision")
    ready = controller.provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)

    clock.advance(timedelta(hours=1))
    expired = controller.reconcile_lease(
        ReconcileLeaseCommand(
            context=_context("sandbox-expiry-reconcile"),
            environment_id=provision.binding.environment.environment_id,
            observed_at=now + timedelta(hours=1),
        )
    )
    empty = controller.reconcile_lease(
        ReconcileLeaseCommand(
            context=_context("sandbox-empty-reconcile"),
            environment_id="10000000-0000-0000-0000-000000009999",
            observed_at=now + timedelta(hours=1),
        )
    )

    assert expired.reconciled_count == 1
    assert expired.items[0].state is MaterializationState.TIMED_OUT
    assert empty.reconciled_count == 0
    assert empty.items == ()


def test_reconcile_rejects_observed_at_later_than_the_trusted_controller_clock(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    repository = tmp_path / "repository-1"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runtime = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
    )
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    environment_id = "10000000-0000-0000-0000-000000000911"

    with pytest.raises(SandboxApplicationError) as denial:
        controller.reconcile_lease(
            ReconcileLeaseCommand(
                context=_context("sandbox-future-authority"),
                environment_id=environment_id,
                observed_at=now + timedelta(microseconds=1),
            )
        )

    assert denial.value.code is DenialCode.RUNTIME_BINDING_INVALID
    assert denial.value.failure_dimension == "observed_at"
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM agent_run.reconciliation_run")).scalar_one() == 0
        )
        audit = db.execute(
            text(
                "SELECT result, reason FROM audit.audit_event "
                "WHERE action='sandbox.lease.reconcile'"
            )
        ).one()
    assert audit == ("DENIED", "RUNTIME_BINDING_INVALID:observed_at")
