from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

from sqlalchemy import text

from control_plane.app.modules.agent_run import (
    EvidenceKind,
    FinalizeExecutionCommand,
    MaterializationGuard,
    MaterializationReady,
    MaterializationState,
    PreviewPublished,
    PublishPreviewCommand,
    SandboxDenied,
)
from control_plane.app.modules.agent_run.adapters import RestrictedDevSandboxAdapter
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import _command, _controller
from tests.agent_run.test_generation_fencing import _context, _evidence
from tests.agent_run.test_sandbox_recovery import _durable_runtime


class _BlockedPreviewRuntime:
    def __init__(self, delegate: RestrictedDevSandboxAdapter) -> None:
        self._delegate = delegate
        self.called = Event()
        self.release = Event()

    def publish_preview(self, *args: Any, **kwargs: Any) -> Any:
        self.called.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("preview barrier timed out")
        return self._delegate.publish_preview(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _CrashAfterPreviewRuntime:
    def __init__(self, delegate: RestrictedDevSandboxAdapter) -> None:
        self._delegate = delegate

    def publish_preview(self, *args: Any, **kwargs: Any) -> Any:
        self._delegate.publish_preview(*args, **kwargs)
        raise SystemExit("simulated process loss after runtime preview")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _preview_ready(
    database: IsolatedAgentRunDatabase,
    tmp_path: Path,
    now: datetime,
) -> tuple[RestrictedDevSandboxAdapter, Any, MaterializationReady]:
    runtime = _durable_runtime(database, tmp_path)
    controller = _controller(database.runtime, runtime, now)
    command = _command(now, key="sandbox-preview-linearization-provision")
    command = command.model_copy(
        update={
            "binding": command.binding.model_copy(
                update={
                    "boundaries": command.binding.boundaries.model_copy(
                        update={"preview_enabled": True}
                    )
                }
            )
        }
    )
    ready = controller.provision_materialization(command)
    assert isinstance(ready, MaterializationReady)
    return runtime, controller, ready


def _guard(ready: MaterializationReady, revision: int) -> MaterializationGuard:
    return MaterializationGuard(
        materialization_id=ready.handle.materialization_id,
        lease_id=ready.handle.lease_id,
        generation=ready.handle.generation,
        fencing_token=ready.handle.fencing_token,
        expected_revision=revision,
    )


def test_preview_intent_linearizes_with_finalize_and_no_access_survives_fence(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime, _, ready = _preview_ready(isolated_agent_run_database, tmp_path, now)
    blocked_runtime = _BlockedPreviewRuntime(runtime)
    preview_controller = _controller(
        isolated_agent_run_database.runtime,
        blocked_runtime,
        now,
    )
    preview_command = PublishPreviewCommand(
        context=_context("sandbox-preview-linearization-publish"),
        guard=_guard(ready, ready.handle.revision),
        metadata=_evidence(EvidenceKind.PREVIEW_METADATA, 7),
        expires_at=now + timedelta(minutes=10),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        preview_future = pool.submit(preview_controller.publish_preview, preview_command)
        assert blocked_runtime.called.wait(timeout=10)
        try:
            with isolated_agent_run_database.owner.connect() as db:
                intent_count = db.execute(
                    text("SELECT count(*) FROM agent_run.preview_intent WHERE state='INTENT'")
                ).scalar_one()
                intent_revision = db.execute(
                    text("SELECT revision FROM agent_run.sandbox_materialization")
                ).scalar_one()
            assert intent_count == 1
            with isolated_agent_run_database.owner.connect() as db:
                assert (
                    db.execute(
                        text(
                            "SELECT count(*) FROM audit.audit_event "
                            "WHERE action='sandbox.preview.intent'"
                        )
                    ).scalar_one()
                    == 1
                )

            finalized = _controller(
                isolated_agent_run_database.runtime,
                runtime,
                now,
            ).finalize_execution(
                FinalizeExecutionCommand(
                    context=_context("sandbox-preview-linearization-finalize"),
                    guard=_guard(ready, intent_revision),
                    evidence_refs=(),
                )
            )
        finally:
            blocked_runtime.release.set()
        preview = preview_future.result(timeout=10)

    assert finalized.state is MaterializationState.FINALIZED
    assert isinstance(preview, SandboxDenied)
    observation = runtime.observe(ready.handle.materialization_id)
    assert observation.preview_access_active is False
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM agent_run.evidence_reference "
                    "WHERE kind='PREVIEW_METADATA'"
                )
            ).scalar_one()
            == 0
        )


def test_preview_takeover_reuses_stable_runtime_result_without_duplicate_evidence(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime, _, ready = _preview_ready(isolated_agent_run_database, tmp_path, now)
    command = PublishPreviewCommand(
        context=_context("sandbox-preview-crash-takeover"),
        guard=_guard(ready, ready.handle.revision),
        metadata=_evidence(EvidenceKind.PREVIEW_METADATA, 8),
        expires_at=now + timedelta(minutes=10),
    )
    crashing = _controller(
        isolated_agent_run_database.runtime,
        _CrashAfterPreviewRuntime(runtime),
        now,
    )

    try:
        crashing.publish_preview(command)
    except SystemExit as error:
        assert "runtime preview" in str(error)
    else:
        raise AssertionError("preview crash was not triggered")

    restarted_runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    recovered = _controller(
        isolated_agent_run_database.runtime,
        restarted_runtime,
        now + timedelta(seconds=31),
    ).publish_preview(command)

    assert isinstance(recovered, PreviewPublished)
    assert restarted_runtime.events == ()
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT state FROM agent_run.preview_intent")).scalar_one()
            == "PUBLISHED"
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM agent_run.evidence_reference "
                    "WHERE kind='PREVIEW_METADATA'"
                )
            ).scalar_one()
            == 1
        )
