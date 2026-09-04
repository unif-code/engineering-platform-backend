from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from control_plane.app.bootstrap.sandbox_controller import create_sandbox_controller_app
from control_plane.app.modules.agent_run import (
    CancelExecutionCommand,
    CancellationReason,
    MaterializationReady,
    MaterializationState,
    ReconcileLeaseCommand,
)
from control_plane.app.modules.agent_run.adapters import (
    RestrictedDevSandboxAdapter,
    SqlAlchemySandboxRepository,
)
from control_plane.app.modules.agent_run.api.runtime import SandboxHttpRuntime
from control_plane.app.modules.agent_run.application.errors import SandboxApplicationError
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_authorization import ScopedAuthorization
from tests.agent_run.test_controller_provision import MutableClock, _command, _controller
from tests.agent_run.test_e2e import _headers, _Verifier
from tests.agent_run.test_generation_fencing import _context
from tests.agent_run.test_sandbox_recovery import _CrashAfterFence, _durable_runtime


@pytest.mark.parametrize("receipt_state", ["COMPLETED", "IN_PROGRESS"])
@pytest.mark.parametrize("grant_old_environment", [False, True])
def test_cancel_receipt_scope_is_its_durable_subject_after_generation_changes(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
    receipt_state: str,
    grant_old_environment: bool,
) -> None:
    now = datetime.now(UTC)
    runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    provision = _command(now, key="sandbox-subject-scope-provision")
    old = controller.provision_materialization(provision)
    assert isinstance(old, MaterializationReady)
    cancel = CancelExecutionCommand(
        context=_context("sandbox-subject-scope-cancel"),
        execution=provision.binding.execution,
        reason=CancellationReason.SECURITY_VIOLATION,
    )
    if receipt_state == "IN_PROGRESS":
        failing = RestrictedDevSandboxAdapter(
            repository_root=tmp_path,
            repositories={"repository-1": tmp_path / "repository-1"},
            state_engine=isolated_agent_run_database.runtime,
            fail_steps=frozenset({"revoke_secret"}),
        )
        result = _controller(isolated_agent_run_database.runtime, failing, now).cancel_execution(
            cancel
        )
        assert result.state is MaterializationState.QUARANTINED
    else:
        assert controller.cancel_execution(cancel).state is MaterializationState.CANCELED
    later = now + timedelta(seconds=31)
    controller = _controller(isolated_agent_run_database.runtime, runtime, later)
    if receipt_state == "IN_PROGRESS":
        reconciled = controller.reconcile_lease(
            ReconcileLeaseCommand(
                context=_context("sandbox-subject-scope-reconcile"),
                environment_id=old.handle.environment_id,
                observed_at=later,
            )
        )
        assert reconciled.items[0].state is MaterializationState.CANCELED
    new_environment = "10000000-0000-0000-0000-000000000955"
    next_provision = provision.model_copy(
        update={
            "context": _context("sandbox-subject-scope-next"),
            "binding": provision.binding.model_copy(
                update={
                    "environment": provision.binding.environment.model_copy(
                        update={
                            "environment_id": new_environment,
                            "workspace_id": "workspace-subject-scope",
                            "requirement_id": "requirement-subject-scope",
                        }
                    )
                }
            ),
        }
    )
    new = controller.provision_materialization(next_provision)
    assert isinstance(new, MaterializationReady)
    assert new.handle.generation == 2
    granted_environment = old.handle.environment_id if grant_old_environment else new_environment
    scoped = _controller(
        isolated_agent_run_database.runtime,
        runtime,
        later,
        authorization=ScopedAuthorization({(cancel.context.actor, granted_environment)}),
    )
    http_runtime = SandboxHttpRuntime(controller=scoped, identity_verifier=_Verifier())
    client = TestClient(create_sandbox_controller_app(runtime_provider=lambda: http_runtime))
    with isolated_agent_run_database.owner.connect() as db:
        before = db.execute(
            text("SELECT * FROM agent_run.command_receipt WHERE idempotency_key=:key"),
            {"key": cancel.context.idempotency_key},
        ).one()
        assert before.state == receipt_state
    if grant_old_environment and receipt_state == "COMPLETED":
        recovered = scoped.cancel_execution(cancel)
        assert recovered.materialization_id == old.handle.materialization_id
        assert recovered.state is MaterializationState.CANCELED
    elif not grant_old_environment:
        with pytest.raises(
            SandboxApplicationError, match="RUNTIME_CAPABILITY_DENIED:workload_scope"
        ):
            scoped.cancel_execution(cancel)
    response = client.post(
        f"/api/v1/internal/sandbox/executions/{old.handle.execution_id}/cancel",
        headers=_headers(cancel.context.idempotency_key),
        json={"reason": "SECURITY_VIOLATION"},
    )
    if grant_old_environment:
        assert response.status_code == 200
        assert response.json()["materializationId"] == old.handle.materialization_id
        assert response.json()["state"] == "CANCELED"
        replayed = scoped.cancel_execution(cancel)
        assert replayed.materialization_id == old.handle.materialization_id
        assert replayed.state is MaterializationState.CANCELED
    else:
        assert response.status_code == 403
        assert response.json()["code"] == "RUNTIME_CAPABILITY_DENIED"
        assert old.handle.materialization_id not in response.text
        assert old.handle.environment_id not in response.text
        assert old.handle.fencing_token.get_secret_value() not in response.text
    with isolated_agent_run_database.owner.connect() as db:
        after = db.execute(
            text("SELECT * FROM agent_run.command_receipt WHERE idempotency_key=:key"),
            {"key": cancel.context.idempotency_key},
        ).one()
        if grant_old_environment:
            assert after.state == "COMPLETED"
        else:
            assert after == before
            assert (
                db.execute(
                    text(
                        "SELECT count(*) FROM audit.audit_event WHERE "
                        "action='sandbox.execution.cancel' AND result='DENIED' "
                        "AND reason='RUNTIME_CAPABILITY_DENIED:workload_scope'"
                    )
                ).scalar_one()
                == 2
            )
        states = db.execute(
            text(
                "SELECT generation, state FROM agent_run.sandbox_materialization "
                "ORDER BY generation"
            )
        ).all()
        assert [tuple(row) for row in states] == [(1, "CANCELED"), (2, "READY")]


def test_cancel_subject_bound_after_preflight_denial_rolls_back_takeover(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    provision = _command(now, key="sandbox-raced-subject-provision")
    old = controller.provision_materialization(provision)
    assert isinstance(old, MaterializationReady)
    cancel = CancelExecutionCommand(
        context=_context("sandbox-raced-subject-cancel"),
        execution=provision.binding.execution,
        reason=CancellationReason.SECURITY_VIOLATION,
    )
    inspected = Event()
    resume = Event()

    class PausedPreflightRepository(SqlAlchemySandboxRepository):
        def idempotency_by_scope(
            self, actor: str, operation: str, idempotency_key: str, *, for_update: bool = False
        ) -> Any:
            row = super().idempotency_by_scope(
                actor, operation, idempotency_key, for_update=for_update
            )
            if not for_update and not inspected.is_set():
                assert row is None
                inspected.set()
                assert resume.wait(timeout=15)
            return row

    new_environment = "10000000-0000-0000-0000-000000000955"
    clock = MutableClock(now)
    scoped = _controller(
        isolated_agent_run_database.runtime,
        runtime,
        now,
        clock=clock,
        repository_factory=PausedPreflightRepository,
        authorization=ScopedAuthorization({(cancel.context.actor, new_environment)}),
    )
    http_runtime = SandboxHttpRuntime(controller=scoped, identity_verifier=_Verifier())
    client = TestClient(create_sandbox_controller_app(runtime_provider=lambda: http_runtime))
    path = f"/api/v1/internal/sandbox/executions/{old.handle.execution_id}/cancel"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            client.post,
            path,
            headers=_headers(cancel.context.idempotency_key),
            json={"reason": "SECURITY_VIOLATION"},
        )
        assert inspected.wait(timeout=15)
        try:
            with pytest.raises(SystemExit, match="runtime fence"):
                _controller(
                    isolated_agent_run_database.runtime, _CrashAfterFence(runtime), now
                ).cancel_execution(cancel)
            later = now + timedelta(seconds=31)
            controller = _controller(isolated_agent_run_database.runtime, runtime, later)
            reconciled = controller.reconcile_lease(
                ReconcileLeaseCommand(
                    context=_context("sandbox-raced-subject-reconcile"),
                    environment_id=old.handle.environment_id,
                    observed_at=later,
                )
            )
            assert reconciled.items[0].state is MaterializationState.CANCELED
            new = controller.provision_materialization(
                provision.model_copy(
                    update={
                        "context": _context("sandbox-raced-subject-next"),
                        "binding": provision.binding.model_copy(
                            update={
                                "environment": provision.binding.environment.model_copy(
                                    update={
                                        "environment_id": new_environment,
                                        "workspace_id": "workspace-raced-subject",
                                        "requirement_id": "requirement-raced-subject",
                                    }
                                )
                            }
                        ),
                    }
                )
            )
            assert isinstance(new, MaterializationReady)
            assert new.handle.generation == 2
            clock.advance(timedelta(seconds=31))
            with isolated_agent_run_database.owner.connect() as db:
                before = db.execute(
                    text("SELECT * FROM agent_run.command_receipt WHERE idempotency_key=:key"),
                    {"key": cancel.context.idempotency_key},
                ).one()
                assert before.state == "IN_PROGRESS"
                assert str(before.subject_id) == old.handle.materialization_id
                assert before.owner_expires_at < later
                leases_before = db.execute(
                    text("SELECT * FROM agent_run.capacity_lease ORDER BY id")
                ).all()
                states_before = db.execute(
                    text("SELECT * FROM agent_run.sandbox_materialization ORDER BY id")
                ).all()
                evidence_before = db.execute(
                    text("SELECT * FROM agent_run.evidence_reference ORDER BY id")
                ).all()
            events_before = runtime.events
        finally:
            resume.set()
        denied = pending.result(timeout=15)
    assert denied.status_code == 403
    assert denied.json()["code"] == "RUNTIME_CAPABILITY_DENIED"
    assert old.handle.materialization_id not in denied.text
    assert old.handle.fencing_token.get_secret_value() not in denied.text
    assert runtime.events == events_before
    with isolated_agent_run_database.owner.connect() as db:
        after = db.execute(
            text("SELECT * FROM agent_run.command_receipt WHERE idempotency_key=:key"),
            {"key": cancel.context.idempotency_key},
        ).one()
        assert after.owner_id == before.owner_id
        assert after.owner_expires_at == before.owner_expires_at
        assert after.state == before.state
        assert after == before
        assert (
            db.execute(text("SELECT * FROM agent_run.capacity_lease ORDER BY id")).all()
            == leases_before
        )
        assert (
            db.execute(text("SELECT * FROM agent_run.sandbox_materialization ORDER BY id")).all()
            == states_before
        )
        assert (
            db.execute(text("SELECT * FROM agent_run.evidence_reference ORDER BY id")).all()
            == evidence_before
        )
        denials = db.execute(
            text("SELECT action, reason FROM audit.audit_event WHERE result='DENIED'")
        ).all()
        assert [tuple(row) for row in denials] == [
            ("sandbox.execution.cancel", "RUNTIME_CAPABILITY_DENIED:workload_scope")
        ]
    old_only = _controller(
        isolated_agent_run_database.runtime,
        runtime,
        later,
        authorization=ScopedAuthorization({(cancel.context.actor, old.handle.environment_id)}),
    )
    recovered = old_only.cancel_execution(cancel)
    assert recovered.materialization_id == old.handle.materialization_id
    assert recovered.state is MaterializationState.CANCELED
