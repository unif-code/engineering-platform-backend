from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from control_plane.app.modules.agent_run import (
    CancelExecutionCommand,
    CancellationReason,
    MaterializationReady,
    MaterializationState,
)
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import _command, _controller, _runtime
from tests.agent_run.test_generation_fencing import _context


def test_cancel_and_timeout_use_the_same_idempotent_cleanup_chain(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    provision = _command(now, key="sandbox-cancel-provision")
    ready = controller.provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)
    cancel = CancelExecutionCommand(
        context=_context("sandbox-cancel-command"),
        execution=provision.binding.execution,
        reason=CancellationReason.CANCELED,
    )

    first = controller.cancel_execution(cancel)
    replay = controller.cancel_execution(cancel)

    assert first == replay
    assert first.state is MaterializationState.CANCELED
    assert first.denial is None
    assert [event.action for event in runtime.events] == [
        "provision",
        "evidence",
        "fence",
        "revoke_secret",
        "destroy",
    ]
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text("SELECT active_attempts, active_units FROM agent_run.capacity_ledger")
        ).one() == (0, 0)
        assert (
            db.execute(text("SELECT state FROM agent_run.capacity_lease")).scalar_one()
            == "RELEASED"
        )


def test_timeout_is_a_cancel_reason_not_a_second_cleanup_implementation(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    provision = _command(now, key="sandbox-timeout-provision")
    ready = controller.provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)

    receipt = controller.cancel_execution(
        CancelExecutionCommand(
            context=_context("sandbox-timeout-command"),
            execution=provision.binding.execution,
            reason=CancellationReason.TIMED_OUT,
        )
    )

    assert receipt.state is MaterializationState.TIMED_OUT
    assert [event.action for event in runtime.events[1:]] == [
        "evidence",
        "fence",
        "revoke_secret",
        "destroy",
    ]
