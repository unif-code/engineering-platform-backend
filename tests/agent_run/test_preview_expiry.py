from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from control_plane.app.bootstrap.sandbox_controller import create_sandbox_controller_app
from control_plane.app.modules.agent_run import EvidenceKind, PublishPreviewCommand, SandboxDenied
from control_plane.app.modules.agent_run.adapters import SqlAlchemySandboxRepository
from control_plane.app.modules.agent_run.api.runtime import SandboxHttpRuntime
from control_plane.app.modules.agent_run.ports.repository import PreviewIntent
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import _controller
from tests.agent_run.test_e2e import _headers, _Verifier
from tests.agent_run.test_generation_fencing import _context, _evidence, _guard
from tests.agent_run.test_preview_concurrency import _preview_ready


class _CrashBeforePreviewIntent(SqlAlchemySandboxRepository):
    def begin_preview_intent(self, **values: Any) -> PreviewIntent:
        raise SystemExit("simulated process loss before preview intent")


@pytest.mark.parametrize("boundary", ["past", "equal", "claim_takeover"])
def test_expired_preview_completes_one_safe_denial_without_publishing_or_repeating_audit(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
    boundary: str,
) -> None:
    now = datetime.now(UTC)
    runtime, controller, ready = _preview_ready(isolated_agent_run_database, tmp_path, now)
    expiry = now + timedelta(
        seconds=20 if boundary == "claim_takeover" else -1 if boundary == "past" else 0
    )
    command = PublishPreviewCommand(
        context=_context("sandbox-expired-preview-original"),
        guard=_guard(ready),
        metadata=_evidence(EvidenceKind.PREVIEW_METADATA, 9),
        expires_at=expiry,
    )
    if boundary == "claim_takeover":
        with pytest.raises(SystemExit, match="before preview intent"):
            _controller(
                isolated_agent_run_database.runtime,
                runtime,
                now,
                repository_factory=_CrashBeforePreviewIntent,
            ).publish_preview(command)
        with isolated_agent_run_database.owner.connect() as db:
            assert db.execute(
                text(
                    "SELECT state, phase, subject_id FROM agent_run.command_receipt "
                    "WHERE operation='sandbox.preview.publish'"
                )
            ).one() == ("IN_PROGRESS", "CLAIMED", None)
        controller = _controller(
            isolated_agent_run_database.runtime, runtime, now + timedelta(seconds=31)
        )
    http_runtime = SandboxHttpRuntime(controller=controller, identity_verifier=_Verifier())
    client = TestClient(create_sandbox_controller_app(runtime_provider=lambda: http_runtime))
    path = f"/api/v1/internal/sandbox/materializations/{ready.handle.materialization_id}/preview"
    body = {
        "leaseId": command.guard.lease_id,
        "generation": command.guard.generation,
        "fencingToken": command.guard.fencing_token.get_secret_value(),
        "metadata": command.metadata.model_dump(mode="json"),
        "expiresAt": command.expires_at.isoformat(),
    }
    headers = {
        **_headers(command.context.idempotency_key, etag='"v2"'),
        "X-Request-ID": "req-previewexpiry",
    }
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "RUNTIME_BINDING_INVALID"
    assert response.json()["failureDimension"] == "preview_expiry"
    with isolated_agent_run_database.owner.connect() as db:
        before_replay = db.execute(
            text(
                "SELECT * FROM agent_run.command_receipt WHERE operation='sandbox.preview.publish'"
            )
        ).one()
        assert (before_replay.state, before_replay.phase, before_replay.owner_id) == (
            "COMPLETED",
            "COMPLETED",
            None,
        )
        audit_before = db.execute(text("SELECT * FROM audit.audit_event ORDER BY id")).all()
    denied = controller.publish_preview(command)
    assert isinstance(denied, SandboxDenied)
    assert denied.denial.code.value == "RUNTIME_BINDING_INVALID"
    assert denied.denial.failure_dimension == "preview_expiry"
    assert controller.publish_preview(command) == denied
    replay = client.post(path, headers=headers, json=body)
    assert replay.status_code == response.status_code
    assert replay.json() == response.json()
    assert [event.action for event in runtime.events] == ["provision"]
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text("SELECT state, revision FROM agent_run.sandbox_materialization")
        ).one() == ("READY", 2)
        assert db.execute(text("SELECT count(*) FROM agent_run.preview_intent")).scalar_one() == 0
        assert (
            db.execute(text("SELECT count(*) FROM agent_run.evidence_reference")).scalar_one() == 0
        )
        assert (
            db.execute(
                text(
                    "SELECT * FROM agent_run.command_receipt "
                    "WHERE operation='sandbox.preview.publish'"
                )
            ).one()
            == before_replay
        )
        assert db.execute(text("SELECT * FROM audit.audit_event ORDER BY id")).all() == audit_before
        denied_rows = db.execute(
            text("SELECT action, result, reason FROM audit.audit_event WHERE result='DENIED'")
        ).all()
        assert [tuple(row) for row in denied_rows] == [
            ("sandbox.preview.publish", "DENIED", "RUNTIME_BINDING_INVALID:preview_expiry")
        ]
