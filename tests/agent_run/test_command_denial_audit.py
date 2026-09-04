from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from control_plane.app.bootstrap.sandbox_controller import create_sandbox_controller_app
from control_plane.app.modules.agent_run import (
    CancelExecutionCommand,
    CancellationReason,
    CheckpointAndReleaseCommand,
    EvidenceKind,
    FinalizeExecutionCommand,
    HandoffToChildCommand,
    MaterializationReady,
    PublishPreviewCommand,
    ReconcileLeaseCommand,
)
from control_plane.app.modules.agent_run.adapters import SqlAlchemySandboxRepository
from control_plane.app.modules.agent_run.api.dto import ProvisionMaterializationRequestDto
from control_plane.app.modules.agent_run.api.runtime import SandboxHttpRuntime
from control_plane.app.shared.idempotency import IdempotencyConflict, canonical_request_fingerprint
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import _command, _controller
from tests.agent_run.test_e2e import _headers, _Verifier
from tests.agent_run.test_generation_fencing import _evidence, _guard
from tests.agent_run.test_sandbox_recovery import _durable_runtime


@pytest.mark.parametrize("conflict", ["live_owner", "fingerprint"])
@pytest.mark.parametrize(
    ("method", "operation", "action", "suffix"),
    [
        ("provision_materialization", "sandbox.provision", "sandbox.materialization.provision", ""),
        ("publish_preview", "sandbox.preview.publish", "sandbox.preview.publish", "preview"),
        (
            "checkpoint_and_release",
            "sandbox.checkpoint_and_release",
            "sandbox.materialization.checkpoint_release",
            "checkpoint-release",
        ),
        (
            "finalize_execution",
            "sandbox.finalize_execution",
            "sandbox.materialization.finalize",
            "finalize",
        ),
        (
            "handoff_to_child",
            "sandbox.handoff_to_child",
            "sandbox.materialization.handoff",
            "handoff",
        ),
        ("cancel_execution", "sandbox.cancel_execution", "sandbox.execution.cancel", "cancel"),
        ("reconcile_lease", "sandbox.reconcile_lease", "sandbox.lease.reconcile", "reconcile"),
    ],
)
def test_each_rejected_command_acquisition_has_one_safe_audit_and_preserves_receipt(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
    conflict: str,
    method: str,
    operation: str,
    action: str,
    suffix: str,
) -> None:
    now = datetime.now(UTC)
    runtime = _durable_runtime(isolated_agent_run_database, tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    provision = _command(now, key="sandbox-audit-seed-provision")
    ready = controller.provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)
    context = provision.context.model_copy(
        update={
            "idempotency_key": "private-command-key-r3",
            "correlation_id": "correlation:r3-attempt",
            "request_id": "request:r3-attempt",
        }
    )
    guard = _guard(ready)
    commands: dict[str, Any] = {
        "provision_materialization": provision.model_copy(update={"context": context}),
        "publish_preview": PublishPreviewCommand(
            context=context,
            guard=guard,
            metadata=_evidence(EvidenceKind.PREVIEW_METADATA, 3),
            expires_at=now + timedelta(minutes=10),
        ),
        "checkpoint_and_release": CheckpointAndReleaseCommand(
            context=context, guard=guard, evidence_refs=()
        ),
        "finalize_execution": FinalizeExecutionCommand(
            context=context, guard=guard, evidence_refs=()
        ),
        "handoff_to_child": HandoffToChildCommand(
            context=context, guard=guard, child_execution_id="private-child-r3"
        ),
        "cancel_execution": CancelExecutionCommand(
            context=context,
            execution=provision.binding.execution,
            reason=CancellationReason.CANCELED,
        ),
        "reconcile_lease": ReconcileLeaseCommand(
            context=context, environment_id=ready.handle.environment_id, observed_at=now
        ),
    }
    command = commands[method]
    root = "/api/v1/internal/sandbox"
    path = f"{root}/materializations/{ready.handle.materialization_id}/{suffix}"
    guard_body: dict[str, Any] = {
        "materializationId": guard.materialization_id,
        "leaseId": guard.lease_id,
        "generation": guard.generation,
        "fencingToken": guard.fencing_token.get_secret_value(),
        "expectedRevision": guard.expected_revision,
    }
    if method == "provision_materialization":
        path = f"{root}/materializations"
        canonical_body = provision.binding.model_dump(mode="json")
        http_body = ProvisionMaterializationRequestDto.from_domain(provision.binding).model_dump(
            mode="json", by_alias=True
        )
    elif method == "cancel_execution":
        path = f"{root}/executions/{ready.handle.execution_id}/cancel"
        canonical_body = {
            "execution": provision.binding.execution.model_dump(mode="json"),
            "reason": "CANCELED",
        }
        http_body = {"reason": "CANCELED"}
    elif method == "reconcile_lease":
        path = f"{root}/leases/reconcile"
        canonical_body = {
            "environmentId": ready.handle.environment_id,
            "executionId": None,
            "observedAt": now.isoformat(),
        }
        http_body = canonical_body
    else:
        extra: dict[str, Any] = (
            {
                "metadata": command.metadata.model_dump(mode="json"),
                "expiresAt": command.expires_at.isoformat(),
            }
            if method == "publish_preview"
            else {"childExecutionId": "private-child-r3"}
            if method == "handoff_to_child"
            else {"evidenceRefs": []}
        )
        canonical_body = {**guard_body, **extra}
        http_body = {
            key: value
            for key, value in canonical_body.items()
            if key not in {"materializationId", "expectedRevision"}
        }
    fingerprint = canonical_request_fingerprint(
        operation=operation,
        method="POST",
        path=path,
        body=canonical_body,
        idempotency_sealing_key=bytes(range(32)),
    )
    stored_fingerprint = fingerprint if conflict == "live_owner" else "0" * 64
    with isolated_agent_run_database.runtime.begin() as db:
        SqlAlchemySandboxRepository(db).acquire_command(
            command_id=str(uuid4()),
            owner_id=str(uuid4()),
            actor=context.actor,
            operation=operation,
            idempotency_key=context.idempotency_key,
            request_fingerprint=stored_fingerprint,
            owner_expires_at=now + timedelta(minutes=1),
            now=now,
        )
    with isolated_agent_run_database.owner.connect() as db:
        before = db.execute(
            text("SELECT * FROM agent_run.command_receipt WHERE idempotency_key=:key"),
            {"key": context.idempotency_key},
        ).one()
        materialization_before = db.execute(
            text("SELECT * FROM agent_run.sandbox_materialization")
        ).one()
    events_before = runtime.events
    with pytest.raises(
        IdempotencyConflict, match="live owner" if conflict == "live_owner" else "different request"
    ):
        getattr(controller, method)(command)
    http_runtime = SandboxHttpRuntime(controller=controller, identity_verifier=_Verifier())
    client = TestClient(create_sandbox_controller_app(runtime_provider=lambda: http_runtime))
    response = client.post(
        path, headers=_headers(context.idempotency_key, etag='"v2"'), json=http_body
    )
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert runtime.events == events_before
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT * FROM agent_run.command_receipt WHERE idempotency_key=:key"),
                {"key": context.idempotency_key},
            ).one()
            == before
        )
        assert (
            db.execute(text("SELECT * FROM agent_run.sandbox_materialization")).one()
            == materialization_before
        )
        denied = db.execute(
            text("SELECT * FROM audit.audit_event WHERE result='DENIED' ORDER BY id")
        ).all()
    assert len(denied) == 2
    assert [row.action for row in denied] == [action, action]
    assert all(row.reason == "IDEMPOTENCY_CONFLICT:command_acquisition" for row in denied)
    audit_text = str(denied)
    for secret in (
        context.idempotency_key,
        stored_fingerprint,
        guard.fencing_token.get_secret_value(),
        "e2e-workload-token",
        "private-child-r3",
    ):
        assert secret not in audit_text
