from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from control_plane.app.bootstrap.app import create_app
from control_plane.app.bootstrap.sandbox_controller import create_sandbox_controller_app
from control_plane.app.modules.agent_run import EvidenceKind, EvidenceRef
from control_plane.app.modules.agent_run.api.dto import ProvisionMaterializationRequestDto
from control_plane.app.modules.agent_run.api.runtime import (
    SandboxHttpRuntime,
    WorkloadPrincipal,
)
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.test_controller_provision import _command, _controller, _runtime


class _Verifier:
    def verify(self, bearer_token: SecretStr) -> WorkloadPrincipal | None:
        if bearer_token.get_secret_value() != "valid-workload-token":
            return None
        return WorkloadPrincipal(actor="workload:orchestrator")


def _app(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
    now: datetime,
) -> FastAPI:
    controller = _controller(
        isolated_agent_run_database.runtime,
        _runtime(tmp_path),
        now,
    )
    runtime = SandboxHttpRuntime(controller=controller, identity_verifier=_Verifier())
    return create_sandbox_controller_app(runtime_provider=lambda: runtime)


def _headers(key: str | None = None, *, etag: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer valid-workload-token"}
    if key is not None:
        headers["Idempotency-Key"] = key
    if etag is not None:
        headers["If-Match"] = etag
    return headers


def test_private_schema_has_exact_eight_authenticated_operations_and_main_app_has_none(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    app = _app(isolated_agent_run_database, tmp_path, datetime.now(UTC))
    schema = app.openapi()
    prefix = "/api/v1/internal/sandbox"
    expected = {
        f"{prefix}/materializations": "post",
        f"{prefix}/materializations/{{materialization_id}}": "get",
        f"{prefix}/materializations/{{materialization_id}}/preview": "post",
        f"{prefix}/materializations/{{materialization_id}}/checkpoint-release": "post",
        f"{prefix}/materializations/{{materialization_id}}/handoff": "post",
        f"{prefix}/materializations/{{materialization_id}}/finalize": "post",
        f"{prefix}/executions/{{execution_id}}/cancel": "post",
        f"{prefix}/leases/reconcile": "post",
    }

    assert set(schema["paths"]) == set(expected)
    for path, method in expected.items():
        operation = schema["paths"][path][method]
        assert operation["security"] == [{"WorkloadBearer": []}]
        if method == "post":
            parameters = {item["name"]: item for item in operation.get("parameters", [])}
            assert parameters["Idempotency-Key"]["required"] is True
        success = next(
            response
            for status, response in operation["responses"].items()
            if status in {"200", "201"}
        )
        assert success["headers"]["ETag"]["schema"]["type"] == "string"

    for suffix in ("preview", "checkpoint-release", "handoff", "finalize"):
        operation = schema["paths"][f"{prefix}/materializations/{{materialization_id}}/{suffix}"][
            "post"
        ]
        parameters = {item["name"]: item for item in operation["parameters"]}
        assert parameters["If-Match"]["required"] is True

    serialized = str(schema).lower()
    for forbidden in (
        "api_key",
        "credential",
        "kata",
        "kubernetes",
        "pod",
        "provider",
        "region",
        "runtimeclass",
        "secret_value",
    ):
        assert forbidden not in serialized
    assert not any(path.startswith(prefix) for path in create_app().openapi()["paths"])


def test_bearer_identity_headers_etags_and_exact_replay(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    client = TestClient(_app(isolated_agent_run_database, tmp_path, now))
    command = _command(now, key="ignored-by-http")
    body = ProvisionMaterializationRequestDto.from_domain(command.binding).model_dump(
        mode="json", by_alias=True
    )

    missing = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers={"Idempotency-Key": "sandbox-api-missing-auth"},
        json=body,
    )
    unknown = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers={
            "Authorization": "Bearer unknown-workload-token",
            "Idempotency-Key": "sandbox-api-unknown-auth",
        },
        json=body,
    )
    first = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers=_headers("sandbox-api-provision"),
        json=body,
    )
    replay = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers=_headers("sandbox-api-provision"),
        json=body,
    )

    assert missing.status_code == 401
    assert unknown.status_code == 401
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert first.status_code == 201
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"] == '"v2"'
    assert first.json()["handle"]["fencingToken"] != "**********"

    materialization_id = first.json()["handle"]["materializationId"]
    status = client.get(
        f"/api/v1/internal/sandbox/materializations/{materialization_id}",
        headers=_headers(),
    )
    assert status.status_code == 200
    assert status.headers["etag"] == '"v2"'
    assert status.json()["materializationId"] == materialization_id


def test_existing_materialization_requires_if_match_and_denials_are_safe_problems(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    client = TestClient(_app(isolated_agent_run_database, tmp_path, now))
    command = _command(now, key="ignored-by-http")
    provision = client.post(
        "/api/v1/internal/sandbox/materializations",
        headers=_headers("sandbox-api-lifecycle-provision"),
        json=ProvisionMaterializationRequestDto.from_domain(command.binding).model_dump(
            mode="json", by_alias=True
        ),
    )
    assert provision.status_code == 201
    handle = provision.json()["handle"]
    materialization_id = handle["materializationId"]
    guard = {
        "leaseId": handle["leaseId"],
        "generation": handle["generation"],
        "fencingToken": handle["fencingToken"],
    }

    missing = client.post(
        f"/api/v1/internal/sandbox/materializations/{materialization_id}/finalize",
        headers=_headers("sandbox-api-finalize-missing-etag"),
        json={**guard, "evidenceRefs": []},
    )
    handoff = client.post(
        f"/api/v1/internal/sandbox/materializations/{materialization_id}/handoff",
        headers=_headers("sandbox-api-handoff", etag=provision.headers["etag"]),
        json={**guard, "childExecutionId": "future-child-execution"},
    )

    assert missing.status_code == 422
    assert handoff.status_code == 403
    assert handoff.headers["content-type"].startswith("application/problem+json")
    assert handoff.json()["code"] == "POLICY_DISABLED"
    assert "exception" not in str(handoff.json()).lower()

    evidence = EvidenceRef(
        kind=EvidenceKind.TEST_RESULT,
        artifact_id="artifact-http-test",
        version="1",
        sha256="sha256:" + "a" * 64,
        classification="INTERNAL",
    )
    finalized = client.post(
        f"/api/v1/internal/sandbox/materializations/{materialization_id}/finalize",
        headers=_headers("sandbox-api-finalize", etag=provision.headers["etag"]),
        json={
            **guard,
            "evidenceRefs": [evidence.model_dump(mode="json")],
        },
    )

    assert finalized.status_code == 200
    assert finalized.json()["state"] == "FINALIZED"
    assert finalized.headers["etag"].startswith('"v')


def test_default_private_runtime_fails_readiness_and_protected_requests_closed() -> None:
    client = TestClient(create_sandbox_controller_app())

    assert client.get("/readyz").status_code == 503
    response = client.get(
        "/api/v1/internal/sandbox/materializations/missing",
        headers={"Authorization": "Bearer unavailable-runtime-token"},
    )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
