from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import text

from control_plane.app.modules.agent_run import (
    CancelExecutionCommand,
    CancellationReason,
    FinalizeExecutionCommand,
    MaterializationReady,
    MaterializationState,
)
from control_plane.app.modules.agent_run.application.errors import SandboxApplicationError
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import _command, _controller
from tests.agent_run.test_generation_fencing import _context, _guard
from tests.agent_run.test_sandbox_recovery import _durable_runtime


class _PausedScopeCheck:
    def __init__(self, environment_id: str) -> None:
        self.environment_id = environment_id
        self.checked = Event()
        self.release = Event()

    def authorize(self, *, actor: str, operation: str, environment_id: str) -> bool:
        assert actor == "workload:orchestrator"
        assert operation == "sandbox.cancel_execution"
        assert environment_id == self.environment_id
        self.checked.set()
        assert self.release.wait(timeout=15)
        return True


@pytest.mark.parametrize("change_environment", [False, True])
def test_cancel_atomically_targets_current_generation_and_revalidates_authorized_scope(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
    change_environment: bool,
) -> None:
    now = datetime.now(UTC)
    runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    provision = _command(now, key="sandbox-cancel-linearization-provision")
    first = controller.provision_materialization(provision)
    assert isinstance(first, MaterializationReady)
    scope = _PausedScopeCheck(first.handle.environment_id)
    cancel_controller = _controller(
        isolated_agent_run_database.runtime, runtime, now, authorization=scope
    )
    cancel = CancelExecutionCommand(
        context=_context("sandbox-cancel-linearization-cancel"),
        execution=provision.binding.execution,
        reason=CancellationReason.TERMINATED,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(cancel_controller.cancel_execution, cancel)
        assert scope.checked.wait(timeout=15)
        try:
            finalized = controller.finalize_execution(
                FinalizeExecutionCommand(
                    context=_context("sandbox-cancel-linearization-finalize"),
                    guard=_guard(first),
                    evidence_refs=(),
                )
            )
            assert finalized.state is MaterializationState.FINALIZED
            binding = provision.binding
            if change_environment:
                binding = binding.model_copy(
                    update={
                        "environment": binding.environment.model_copy(
                            update={
                                "environment_id": "10000000-0000-0000-0000-000000000955",
                                "workspace_id": "workspace-cancel-race",
                                "requirement_id": "requirement-cancel-race",
                            }
                        )
                    }
                )
            second = controller.provision_materialization(
                provision.model_copy(
                    update={
                        "context": _context("sandbox-cancel-linearization-next"),
                        "binding": binding,
                    }
                )
            )
            assert isinstance(second, MaterializationReady)
            assert second.handle.generation == 2
        finally:
            scope.release.set()
        if change_environment:
            with pytest.raises(
                SandboxApplicationError, match="RUNTIME_CAPABILITY_DENIED:workload_scope"
            ):
                pending.result(timeout=15)
        else:
            result = pending.result(timeout=15)
            assert result.materialization_id == second.handle.materialization_id
            assert result.state is MaterializationState.CANCELED
    with isolated_agent_run_database.owner.connect() as db:
        states = db.execute(
            text(
                "SELECT generation, state FROM agent_run.sandbox_materialization "
                "ORDER BY generation"
            )
        ).all()
        assert [tuple(row) for row in states] == [
            (1, "FINALIZED"),
            (2, "READY" if change_environment else "CANCELED"),
        ]
        if change_environment:
            assert (
                db.execute(
                    text(
                        "SELECT count(*) FROM audit.audit_event "
                        "WHERE action='sandbox.execution.cancel' AND result='DENIED'"
                    )
                ).scalar_one()
                == 1
            )
