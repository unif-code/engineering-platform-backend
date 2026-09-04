from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from control_plane.app.bootstrap.sandbox_controller import create_sandbox_controller_app
from control_plane.app.modules.agent_run import (
    CancelExecutionCommand,
    CancellationReason,
    CheckpointAndReleaseCommand,
    CommandContext,
    EvidenceKind,
    ExecutionRef,
    FinalizeExecutionCommand,
    GetMaterializationStatusQuery,
    HandoffToChildCommand,
    MaterializationReady,
    PublishPreviewCommand,
    ReconcileLeaseCommand,
)
from control_plane.app.modules.agent_run.api.runtime import SandboxHttpRuntime, WorkloadPrincipal
from control_plane.app.modules.agent_run.application.errors import SandboxApplicationError
from control_plane.app.modules.agent_run.ports.services import DefaultDenyWorkloadAuthorization
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import _command, _controller, _runtime
from tests.agent_run.test_generation_fencing import _evidence, _guard


class ScopedAuthorization:
    def __init__(self, grants: set[tuple[str, str]]) -> None:
        self._grants = grants

    def authorize(self, *, actor: str, operation: str, environment_id: str) -> bool:
        return (actor, environment_id) in self._grants


def test_distinct_principal_cannot_read_cancel_or_reconcile_an_ungranted_environment(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    environment_id = "10000000-0000-0000-0000-000000000911"
    execution_id = "execution-authorization-1"
    authorization = ScopedAuthorization({("workload:owner", environment_id)})
    controller = _controller(
        isolated_agent_run_database.runtime,
        _runtime(tmp_path),
        now,
        authorization=authorization,
    )
    provision = _command(
        now,
        key="authorization-provision-owner",
        execution_id=execution_id,
        environment_id=environment_id,
    )
    provision = provision.model_copy(
        update={"context": provision.context.model_copy(update={"actor": "workload:owner"})}
    )
    ready = controller.provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)

    intruder = "workload:intruder"
    with pytest.raises(SandboxApplicationError) as status_denial:
        controller.get_materialization_status(
            GetMaterializationStatusQuery(
                actor=intruder,
                materialization_id=ready.handle.materialization_id,
            )
        )
    with pytest.raises(SandboxApplicationError) as cancel_denial:
        controller.cancel_execution(
            CancelExecutionCommand(
                context=CommandContext(
                    idempotency_key="authorization-cancel-intruder",
                    actor=intruder,
                    correlation_id="correlation:authorization-cancel-intruder",
                ),
                execution=ExecutionRef(
                    execution_id=execution_id, kind=provision.binding.execution.kind
                ),
                reason=CancellationReason.SECURITY_VIOLATION,
            )
        )
    with pytest.raises(SandboxApplicationError) as reconcile_denial:
        controller.reconcile_lease(
            ReconcileLeaseCommand(
                context=CommandContext(
                    idempotency_key="authorization-reconcile-intruder",
                    actor=intruder,
                    correlation_id="correlation:authorization-reconcile-intruder",
                ),
                environment_id=environment_id,
                execution_id=execution_id,
                observed_at=now,
            )
        )

    assert {
        error.value.code.value for error in (status_denial, cancel_denial, reconcile_denial)
    } == {"RUNTIME_CAPABILITY_DENIED"}
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT state FROM agent_run.sandbox_materialization")).scalar_one()
            == "READY"
        )
        denials = db.execute(
            text(
                "SELECT action, result, reason FROM audit.audit_event "
                "WHERE actor=:actor ORDER BY occurred_at, id"
            ),
            {"actor": intruder},
        ).all()
    assert {row.action for row in denials} == {
        "sandbox.materialization.status",
        "sandbox.execution.cancel",
        "sandbox.lease.reconcile",
    }
    assert all(row.result == "DENIED" for row in denials)
    assert all(row.reason == "RUNTIME_CAPABILITY_DENIED:workload_scope" for row in denials)


@pytest.mark.parametrize(
    "method",
    [
        "provision_materialization",
        "get_materialization_status",
        "publish_preview",
        "checkpoint_and_release",
        "handoff_to_child",
        "finalize_execution",
        "cancel_execution",
        "reconcile_lease",
    ],
)
def test_every_operation_default_denies_without_explicit_scope_grant(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
    method: str,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    allowed = _controller(isolated_agent_run_database.runtime, runtime, now)
    provision = _command(now, key="sandbox-default-deny-provision")
    ready = allowed.provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)
    denied = _controller(
        isolated_agent_run_database.runtime,
        runtime,
        now,
        authorization=DefaultDenyWorkloadAuthorization(),
    )
    context = provision.context.model_copy(
        update={"idempotency_key": "sandbox-default-deny-command"}
    )
    commands: dict[str, Any] = {
        "provision_materialization": provision,
        "get_materialization_status": GetMaterializationStatusQuery(
            actor=context.actor, materialization_id=ready.handle.materialization_id
        ),
        "publish_preview": PublishPreviewCommand(
            context=context,
            guard=_guard(ready),
            metadata=_evidence(EvidenceKind.PREVIEW_METADATA, 1),
            expires_at=ready.handle.deadline_at,
        ),
        "checkpoint_and_release": CheckpointAndReleaseCommand(
            context=context, guard=_guard(ready), evidence_refs=()
        ),
        "handoff_to_child": HandoffToChildCommand(
            context=context, guard=_guard(ready), child_execution_id="child-1"
        ),
        "finalize_execution": FinalizeExecutionCommand(
            context=context, guard=_guard(ready), evidence_refs=()
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
    with pytest.raises(SandboxApplicationError, match="RUNTIME_CAPABILITY_DENIED:workload_scope"):
        getattr(denied, method)(commands[method])
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT state FROM agent_run.sandbox_materialization")).scalar_one()
            == "READY"
        )
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE result='DENIED'")
            ).scalar_one()
            == 1
        )


class _TwoPrincipalVerifier:
    def verify(self, bearer_token: SecretStr) -> WorkloadPrincipal | None:
        token = bearer_token.get_secret_value()
        if token in {"owner-test-token", "intruder-test-token"}:
            return WorkloadPrincipal(actor="workload:" + token.split("-")[0])
        return None


def test_http_authentication_does_not_grant_cross_environment_read_cancel_or_reconcile(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    provision = _command(now, key="sandbox-http-scope-provision")
    provision = provision.model_copy(
        update={"context": provision.context.model_copy(update={"actor": "workload:owner"})}
    )
    controller = _controller(
        isolated_agent_run_database.runtime,
        _runtime(tmp_path),
        now,
        authorization=ScopedAuthorization(
            {("workload:owner", provision.binding.environment.environment_id)}
        ),
    )
    ready = controller.provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)
    runtime = SandboxHttpRuntime(controller=controller, identity_verifier=_TwoPrincipalVerifier())
    client = TestClient(create_sandbox_controller_app(runtime_provider=lambda: runtime))
    prefix = "/api/v1/internal/sandbox"
    headers = {
        "Authorization": "Bearer intruder-test-token",
        "Idempotency-Key": "sandbox-http-scope-denied",
    }
    responses = [
        client.get(f"{prefix}/materializations/{ready.handle.materialization_id}", headers=headers),
        client.post(
            f"{prefix}/executions/{ready.handle.execution_id}/cancel",
            headers=headers,
            json={"reason": "CANCELED"},
        ),
        client.post(
            f"{prefix}/leases/reconcile",
            headers=headers,
            json={"environmentId": ready.handle.environment_id, "observedAt": now.isoformat()},
        ),
    ]
    assert [response.status_code for response in responses] == [403, 403, 403]
    assert all(response.json()["code"] == "RUNTIME_CAPABILITY_DENIED" for response in responses)
    allowed = client.get(
        f"{prefix}/materializations/{ready.handle.materialization_id}",
        headers={"Authorization": "Bearer owner-test-token"},
    )
    assert allowed.status_code == 200
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT state FROM agent_run.sandbox_materialization")).scalar_one()
            == "READY"
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE actor='workload:intruder' AND result='DENIED'"
                )
            ).scalar_one()
            == 3
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='sandbox.materialization.status' AND result='SUCCEEDED'"
                )
            ).scalar_one()
            == 1
        )


class _ReconcileOnlyGrant:
    def authorize(self, *, actor: str, operation: str, environment_id: str) -> bool:
        return actor == "workload:orchestrator" and operation == "sandbox.reconcile_lease"


def test_reconcile_grant_does_not_require_an_unrelated_public_status_grant(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    provision = _command(now, key="sandbox-logical-operation-grant")
    ready = _controller(
        isolated_agent_run_database.runtime, runtime, now
    ).provision_materialization(provision)
    assert isinstance(ready, MaterializationReady)
    expired = now + timedelta(hours=1)
    controller = _controller(
        isolated_agent_run_database.runtime,
        runtime,
        expired,
        authorization=_ReconcileOnlyGrant(),
    )
    receipt = controller.reconcile_lease(
        ReconcileLeaseCommand(
            context=provision.context.model_copy(
                update={"idempotency_key": "sandbox-logical-operation-reconcile"}
            ),
            environment_id=ready.handle.environment_id,
            observed_at=expired,
        )
    )
    assert receipt.reconciled_count == 1


def test_unknown_target_canonical_status_denial_is_audited_without_evidence(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    controller = _controller(
        isolated_agent_run_database.runtime, _runtime(tmp_path), datetime.now(UTC)
    )
    with pytest.raises(SandboxApplicationError, match="STALE_RUNNER_GENERATION"):
        controller.get_materialization_status(
            GetMaterializationStatusQuery(
                actor="workload:orchestrator",
                materialization_id="10000000-0000-0000-0000-000000009999",
            )
        )
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text("SELECT action, result, reason FROM audit.audit_event WHERE result='DENIED'")
        ).one() == (
            "sandbox.materialization.status",
            "DENIED",
            "STALE_RUNNER_GENERATION:runner_generation",
        )
