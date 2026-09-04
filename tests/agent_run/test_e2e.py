from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr
from sqlalchemy import text

from control_plane.app.bootstrap.sandbox_controller import create_sandbox_controller_app
from control_plane.app.modules.agent_run import EvidenceKind, EvidenceRef
from control_plane.app.modules.agent_run.adapters import RestrictedDevSandboxAdapter
from control_plane.app.modules.agent_run.api.dto import ProvisionMaterializationRequestDto
from control_plane.app.modules.agent_run.api.runtime import SandboxHttpRuntime, WorkloadPrincipal
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import MutableClock, _command, _controller, _runtime


class _Verifier:
    def verify(self, bearer_token: SecretStr) -> WorkloadPrincipal | None:
        if bearer_token.get_secret_value() == "e2e-workload-token":
            return WorkloadPrincipal(actor="workload:orchestrator")
        return None


def _headers(key: str | None = None, *, etag: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer e2e-workload-token"}
    if key is not None:
        headers["Idempotency-Key"] = key
    if etag is not None:
        headers["If-Match"] = etag
    return headers


def _body(now: datetime, *, execution_id: str = "execution-e2e-1") -> dict[str, Any]:
    command = _command(now, key="ignored-by-http", execution_id=execution_id)
    return dict(
        ProvisionMaterializationRequestDto.from_domain(command.binding).model_dump(
            mode="json", by_alias=True
        )
    )


def _client(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    runtime: RestrictedDevSandboxAdapter,
    now: datetime,
    *,
    clock: MutableClock | None = None,
) -> TestClient:
    controller = _controller(isolated_agent_run_database.runtime, runtime, now, clock=clock)
    app_runtime = SandboxHttpRuntime(controller=controller, identity_verifier=_Verifier())
    return TestClient(create_sandbox_controller_app(runtime_provider=lambda: app_runtime))


def _guard(handle: dict[str, Any]) -> dict[str, Any]:
    return {
        "leaseId": handle["leaseId"],
        "generation": handle["generation"],
        "fencingToken": handle["fencingToken"],
    }


def test_authorized_http_journey_replays_and_releases_without_plaintext_secrets(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    client = _client(isolated_agent_run_database, runtime, now)
    body = _body(now)

    provision = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers=_headers("sandbox-e2e-provision"),
        json=body,
    )
    replay = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers=_headers("sandbox-e2e-provision"),
        json=body,
    )
    assert provision.status_code == replay.status_code == 201
    assert provision.json() == replay.json()
    handle = provision.json()["handle"]
    materialization_id = handle["materializationId"]
    token = handle["fencingToken"]

    status = client.get(
        f"/api/v1/internal/sandbox/materializations/{materialization_id}",
        headers=_headers(),
    )
    evidence = EvidenceRef(
        kind=EvidenceKind.TEST_RESULT,
        artifact_id="artifact-e2e-test",
        version="1",
        sha256="sha256:" + "b" * 64,
        classification="INTERNAL",
    )
    finalized = client.post(
        f"/api/v1/internal/sandbox/materializations/{materialization_id}/finalize",
        headers=_headers("sandbox-e2e-finalize", etag=status.headers["etag"]),
        json={
            **_guard(handle),
            "evidenceRefs": [evidence.model_dump(mode="json")],
        },
    )

    assert status.status_code == 200
    assert finalized.status_code == 200
    assert finalized.json()["state"] == "FINALIZED"
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text("SELECT active_attempts, active_units FROM agent_run.capacity_ledger")
        ).one() == (0, 0)
        assert (
            db.execute(text("SELECT count(*) FROM agent_run.runner_generation")).scalar_one() == 1
        )
        ciphertext = db.execute(
            text(
                "SELECT sealed_response FROM agent_run.command_receipt "
                "WHERE operation='sandbox.provision'"
            )
        ).scalar_one()
        audit_text = " ".join(
            str(value)
            for row in db.execute(
                text(
                    "SELECT actor, action, target_type, target_id, result, reason "
                    "FROM audit.audit_event"
                )
            )
            for value in row
            if value is not None
        )
        schema_columns = {
            column
            for (column,) in db.execute(
                text(
                    "SELECT lower(table_name || '.' || column_name) "
                    "FROM information_schema.columns WHERE table_schema='agent_run'"
                )
            )
        }
    assert token.encode("utf-8") not in ciphertext
    assert token not in audit_text
    assert "e2e-workload-token" not in audit_text
    for forbidden in ("credential", "kata", "kubernetes", "pod", "provider", "region"):
        assert not any(forbidden in column for column in schema_columns)


@pytest.mark.parametrize("cross_environment", [False, True])
def test_concurrent_http_provision_has_one_generation_and_one_conflict(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
    cross_environment: bool,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    client = _client(isolated_agent_run_database, runtime, now)
    barrier = Barrier(2)

    def provision(number: int) -> Response:
        body = _body(now, execution_id="execution-e2e-concurrent")
        if cross_environment:
            body["environment"].update(
                {
                    "environmentId": f"10000000-0000-0000-0000-00000000099{number}",
                    "workspaceId": f"workspace-race-{number}",
                    "requirementId": f"requirement-race-{number}",
                }
            )
        barrier.wait(timeout=10)
        return cast(
            Response,
            client.post(
                "/api/v1/internal/sandbox/materializations",
                headers=_headers(f"sandbox-e2e-concurrent-{number}"),
                json=body,
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(provision, (1, 2)))

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["code"] == "RUNTIME_BINDING_INVALID"
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM agent_run.sandbox_materialization")).scalar_one()
            == 1
        )
        assert (
            db.execute(text("SELECT count(*) FROM agent_run.runner_generation")).scalar_one() == 1
        )
        assert db.execute(
            text(
                "SELECT result, reason FROM audit.audit_event "
                "WHERE action='sandbox.materialization.provision' AND result='DENIED'"
            )
        ).one() == ("DENIED", "RUNTIME_BINDING_INVALID:active_execution")


def test_stale_generation_and_malicious_boundary_are_denied_and_audited(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    client = _client(isolated_agent_run_database, runtime, now)
    body = _body(now, execution_id="execution-e2e-attack")
    provision = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers=_headers("sandbox-e2e-attack-provision"),
        json=body,
    )
    assert provision.status_code == 201
    handle = provision.json()["handle"]
    materialization_id = handle["materializationId"]
    preview_metadata = EvidenceRef(
        kind=EvidenceKind.PREVIEW_METADATA,
        artifact_id="artifact-e2e-preview",
        version="1",
        sha256="sha256:" + "c" * 64,
        classification="INTERNAL",
    )

    stale = client.post(
        f"/api/v1/internal/sandbox/materializations/{materialization_id}/preview",
        headers=_headers("sandbox-e2e-stale", etag='"v1"'),
        json={
            **_guard(handle),
            "metadata": preview_metadata.model_dump(mode="json"),
            "expiresAt": (now + timedelta(minutes=5)).isoformat(),
        },
    )
    malicious_body = _body(now, execution_id="execution-e2e-malicious")
    malicious_body["boundaries"]["allowedNetworkTargetRefs"] = [
        "gateway:model",
        "https://unapproved.invalid",
    ]
    malicious = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers=_headers("sandbox-e2e-malicious"),
        json=malicious_body,
    )

    assert stale.status_code == 409
    assert stale.json()["code"] == "STALE_RUNNER_GENERATION"
    assert malicious.status_code == 403
    assert malicious.json()["code"] == "RUNTIME_BOUNDARY_VIOLATION"
    with isolated_agent_run_database.owner.connect() as db:
        reasons = set(
            db.execute(
                text("SELECT reason FROM audit.audit_event WHERE reason IS NOT NULL")
            ).scalars()
        )
    assert "STALE_RUNNER_GENERATION:runner_generation" in reasons
    assert "RUNTIME_BOUNDARY_VIOLATION:network" in reasons


def test_http_cancel_quarantines_then_reconciliation_completes_missing_steps(
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
        fail_steps=frozenset({"revoke_secret"}),
    )
    clock = MutableClock(now)
    client = _client(isolated_agent_run_database, runtime, now, clock=clock)
    execution_id = "execution-e2e-reconcile"
    body = _body(now, execution_id=execution_id)
    provision = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers=_headers("sandbox-e2e-reconcile-provision"),
        json=body,
    )
    assert provision.status_code == 201

    canceled = client.post(
        f"/api/v1/internal/sandbox/executions/{execution_id}/cancel",
        headers=_headers("sandbox-e2e-reconcile-cancel"),
        json={"reason": "CANCELED"},
    )
    assert canceled.status_code == 503
    assert canceled.json()["code"] == "RESOURCE_EXHAUSTED"

    runtime.clear_failures()
    clock.advance(timedelta(hours=1))
    reconciled = client.post(
        "/api/v1/internal/sandbox/leases/reconcile",
        headers=_headers("sandbox-e2e-reconcile-run"),
        json={
            "environmentId": body["environment"]["environmentId"],
            "executionId": execution_id,
            "observedAt": (now + timedelta(hours=1)).isoformat(),
        },
    )

    assert reconciled.status_code == 200
    assert reconciled.json()["reconciledCount"] == 1
    assert reconciled.json()["items"][0]["state"] == "CANCELED"
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
