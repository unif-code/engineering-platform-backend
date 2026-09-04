from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from control_plane.app.modules.agent_run import (
    CheckpointAndReleaseCommand,
    CommandContext,
    DenialCode,
    EvidenceKind,
    EvidenceRef,
    FinalizeExecutionCommand,
    GetMaterializationStatusQuery,
    HandoffToChildCommand,
    LifecycleReceipt,
    MaterializationGuard,
    MaterializationReady,
    MaterializationState,
    PreviewPublished,
    PublishPreviewCommand,
    SandboxDenied,
)
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import _command, _controller, _runtime


def _context(key: str) -> CommandContext:
    return CommandContext(
        idempotency_key=key,
        actor="workload:orchestrator",
        correlation_id=f"correlation:{key}",
        request_id=f"request:{key}",
    )


def _guard(ready: MaterializationReady) -> MaterializationGuard:
    return MaterializationGuard(
        materialization_id=ready.handle.materialization_id,
        lease_id=ready.handle.lease_id,
        generation=ready.handle.generation,
        fencing_token=ready.handle.fencing_token,
        expected_revision=ready.handle.revision,
    )


def _evidence(kind: EvidenceKind, number: int) -> EvidenceRef:
    return EvidenceRef(
        kind=kind,
        artifact_id=f"artifact-{number}",
        version="1",
        sha256="sha256:" + str(number) * 64,
        classification="INTERNAL",
    )


def test_status_and_bound_preview_use_logical_refs_and_stale_revision_is_denied(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    provision = _command(now, key="sandbox-preview-provision")
    provision = provision.model_copy(
        update={
            "binding": provision.binding.model_copy(
                update={
                    "boundaries": provision.binding.boundaries.model_copy(
                        update={"preview_enabled": True}
                    )
                }
            )
        }
    )
    ready = controller.provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)

    status = controller.get_materialization_status(
        GetMaterializationStatusQuery(
            actor="workload:orchestrator",
            materialization_id=ready.handle.materialization_id,
        )
    )
    preview = controller.publish_preview(
        PublishPreviewCommand(
            context=_context("sandbox-preview-publish"),
            guard=_guard(ready),
            metadata=_evidence(EvidenceKind.PREVIEW_METADATA, 1),
            expires_at=now + timedelta(minutes=10),
        )
    )
    stale = controller.publish_preview(
        PublishPreviewCommand(
            context=_context("sandbox-preview-stale"),
            guard=_guard(ready),
            metadata=_evidence(EvidenceKind.PREVIEW_METADATA, 2),
            expires_at=now + timedelta(minutes=10),
        )
    )

    assert status.state is MaterializationState.READY
    assert status.revision == 2
    assert isinstance(preview, PreviewPublished)
    assert preview.preview_id.startswith("preview:")
    assert preview.access_ref.startswith("preview-access:")
    assert preview.revision == 3
    assert isinstance(stale, SandboxDenied)
    assert stale.denial.code is DenialCode.STALE_RUNNER_GENERATION
    assert [event.action for event in runtime.events] == ["provision", "publish_preview"]
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM agent_run.evidence_reference")).scalar_one() == 1
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE reason='STALE_RUNNER_GENERATION:runner_generation'"
                )
            ).scalar_one()
            == 1
        )


def test_finalize_persists_ordered_evidence_then_fences_revokes_releases_and_destroys(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    ready = controller.provision_materialization(_command(now, key="sandbox-finalize-provision"))
    assert isinstance(ready, MaterializationReady)

    receipt = controller.finalize_execution(
        FinalizeExecutionCommand(
            context=_context("sandbox-finalize-command"),
            guard=_guard(ready),
            evidence_refs=(
                _evidence(EvidenceKind.PATCH, 2),
                _evidence(EvidenceKind.TEST_RESULT, 3),
            ),
        )
    )

    assert isinstance(receipt, LifecycleReceipt)
    assert receipt.state is MaterializationState.FINALIZED
    assert receipt.denial is None
    assert receipt.evidence_refs == (
        _evidence(EvidenceKind.PATCH, 2),
        _evidence(EvidenceKind.TEST_RESULT, 3),
    )
    assert [event.action for event in runtime.events] == [
        "provision",
        "evidence",
        "fence",
        "revoke_secret",
        "destroy",
    ]
    with isolated_agent_run_database.owner.connect() as db:
        evidence = db.execute(
            text(
                "SELECT sequence, kind, artifact_id FROM agent_run.evidence_reference "
                "ORDER BY sequence"
            )
        ).all()
        cleanup = db.execute(
            text(
                "SELECT state, evidence_persisted_at <= fenced_at, "
                "fenced_at <= secret_revoked_at, secret_revoked_at <= lease_released_at, "
                "lease_released_at <= destroyed_at FROM agent_run.sandbox_materialization"
            )
        ).one()
        lease_state = db.execute(text("SELECT state FROM agent_run.capacity_lease")).scalar_one()
    assert [tuple(row) for row in evidence] == [
        (1, "PATCH", "artifact-2"),
        (2, "TEST_RESULT", "artifact-3"),
    ]
    assert cleanup == ("FINALIZED", True, True, True, True)
    assert lease_state == "RELEASED"


def test_checkpoint_release_and_v09_handoff_have_bounded_semantics(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    ready = controller.provision_materialization(_command(now, key="sandbox-checkpoint-provision"))
    assert isinstance(ready, MaterializationReady)

    handoff = controller.handoff_to_child(
        HandoffToChildCommand(
            context=_context("sandbox-handoff-disabled"),
            guard=_guard(ready),
            child_execution_id="future-child-execution",
        )
    )
    released = controller.checkpoint_and_release(
        CheckpointAndReleaseCommand(
            context=_context("sandbox-checkpoint-release"),
            guard=_guard(ready),
            evidence_refs=(_evidence(EvidenceKind.CHECKPOINT, 4),),
        )
    )

    assert handoff.state is MaterializationState.READY
    assert handoff.denial is not None
    assert handoff.denial.code is DenialCode.POLICY_DISABLED
    assert released.state is MaterializationState.RELEASED
    with isolated_agent_run_database.owner.connect() as db:
        child_tables = [
            name
            for (name,) in db.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='agent_run' "
                    "AND tablename LIKE '%child%'"
                )
            )
        ]
    assert child_tables == []
