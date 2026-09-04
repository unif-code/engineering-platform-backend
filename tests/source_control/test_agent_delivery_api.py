from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.source_control import authorize_agent_push
from control_plane.app.modules.source_control.api import (
    SourceControlQueryRuntime,
    create_agent_delivery_query_router,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from tests.source_control.test_agent_delivery_adapters import (
    ATTEMPT_ID,
    BRANCH_BINDING_ID,
    BRANCH_NAME,
    CONTENT_DIGEST,
    EXPECTED_HEAD,
    REPOSITORY_ID,
    REQUIREMENT_ID,
    TARGET_COMMIT,
    WORK_ITEM_ID,
    WORKSPACE_ID,
    _seed_branch,
)
from tests.source_control.test_agent_delivery_commands import (
    RAW_FENCE,
    RAW_GRANT,
    _dependencies,
    _spec,
)

OTHER_WORKSPACE_ID = "20000000-0000-0000-0000-000000000399"
MISSING_DELIVERY_ID = "93000000-0000-0000-0000-000000000399"


@dataclass(frozen=True, slots=True)
class Principal:
    account_id: str


@dataclass(slots=True)
class CapabilityGuard:
    allowed: set[tuple[str, str | None]]
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def __call__(self, principal: Any, capability: str, workspace_id: str | None) -> None:
        del principal
        key = (capability, workspace_id)
        self.calls.append(key)
        if key not in self.allowed:
            raise HTTPException(status_code=403, detail="Forbidden")


def _client(
    engine: Engine,
    *,
    dependencies: Any,
    guard: CapabilityGuard,
    runtime_provider: Any | None = None,
) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_agent_delivery_query_router(
            runtime_provider
            or (lambda: SourceControlQueryRuntime(engine=engine, dependencies=dependencies)),
            lambda: Principal("employee-1"),
            guard,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def _authorize_delivery(engine: Engine) -> tuple[Any, Any]:
    _seed_branch(engine)
    dependencies, *_ = _dependencies(engine)
    result = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    return dependencies, result


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested_key for nested in value.values() for nested_key in _all_keys(nested)
        }
    if isinstance(value, list):
        return {nested_key for nested in value for nested_key in _all_keys(nested)}
    return set()


def test_agent_delivery_query_is_workspace_scoped_and_returns_only_safe_facts(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, grant = _authorize_delivery(isolated_source_control_rw_engine)
    guard = CapabilityGuard({("requirement.read", WORKSPACE_ID)})
    client = _client(
        isolated_source_control_rw_engine,
        dependencies=dependencies,
        guard=guard,
    )
    delivery_queries: list[str] = []

    def capture_delivery_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "source_control.agent_push_request" in statement:
            delivery_queries.append(statement.lower())

    event.listen(
        isolated_source_control_rw_engine,
        "before_cursor_execute",
        capture_delivery_query,
    )

    try:
        response = client.get(
            f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{grant.delivery.id}"
        )
    finally:
        event.remove(
            isolated_source_control_rw_engine,
            "before_cursor_execute",
            capture_delivery_query,
        )

    assert response.status_code == 200
    assert response.json() == {
        "deliveryId": grant.delivery.id,
        "attemptId": ATTEMPT_ID,
        "attemptGeneration": 3,
        "requirementId": REQUIREMENT_ID,
        "workItemId": WORK_ITEM_ID,
        "workspaceId": WORKSPACE_ID,
        "repositoryId": REPOSITORY_ID,
        "branchBindingId": BRANCH_BINDING_ID,
        "branchName": BRANCH_NAME,
        "expectedRemoteHeadSha": EXPECTED_HEAD,
        "targetCommitSha": TARGET_COMMIT,
        "contentDigest": CONTENT_DIGEST,
        "artifactRefs": ["artifact://patch/301"],
        "state": "AUTHORIZED",
        "issuedAt": "2026-08-31T03:00:00Z",
        "expiresAt": "2026-08-31T03:01:00Z",
        "consumedAt": None,
        "observedAt": None,
        "completedAt": None,
        "remoteHeadSha": None,
        "lastErrorCode": None,
        "correlationId": "correlation-command-301",
    }
    assert guard.calls == [("requirement.read", WORKSPACE_ID)]
    assert RAW_GRANT not in response.text
    assert RAW_FENCE not in response.text
    assert len(delivery_queries) == 1
    assert "workspace_id" in delivery_queries[0]
    assert "grant_digest" not in delivery_queries[0]
    assert "execution_binding_digest" not in delivery_queries[0]
    assert "select *" not in delivery_queries[0]
    response_keys = _all_keys(response.json())
    for forbidden in (
        "grantDigest",
        "executionBindingDigest",
        "requestFingerprint",
        "fencingToken",
        "credential",
        "environment",
        "command",
    ):
        assert forbidden not in response_keys


def test_agent_delivery_query_requires_capability_before_lookup(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, grant = _authorize_delivery(isolated_source_control_rw_engine)
    guard = CapabilityGuard(set())

    response = _client(
        isolated_source_control_rw_engine,
        dependencies=dependencies,
        guard=guard,
    ).get(f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{grant.delivery.id}")

    assert response.status_code == 403
    assert guard.calls == [("requirement.read", WORKSPACE_ID)]


def test_missing_and_cross_workspace_delivery_have_identical_not_found_response(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, grant = _authorize_delivery(isolated_source_control_rw_engine)
    guard = CapabilityGuard(
        {
            ("requirement.read", WORKSPACE_ID),
            ("requirement.read", OTHER_WORKSPACE_ID),
        }
    )
    client = _client(
        isolated_source_control_rw_engine,
        dependencies=dependencies,
        guard=guard,
    )
    headers = {"X-Request-ID": "req-agentdeliverynotfound"}

    missing = client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{MISSING_DELIVERY_ID}",
        headers=headers,
    )
    cross_workspace = client.get(
        f"/api/v1/workspaces/{OTHER_WORKSPACE_ID}/agent-deliveries/{grant.delivery.id}",
        headers=headers,
    )

    assert missing.status_code == cross_workspace.status_code == 404
    assert (
        missing.json()
        == cross_workspace.json()
        == {
            "title": "Agent delivery not found",
            "status": 404,
            "requestId": "req-agentdeliverynotfound",
        }
    )


def test_agent_delivery_query_fails_closed_when_sql_is_unavailable(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, _grant = _authorize_delivery(isolated_source_control_rw_engine)
    guard = CapabilityGuard({("requirement.read", WORKSPACE_ID)})

    def unavailable_runtime() -> SourceControlQueryRuntime:
        raise SQLAlchemyError("unavailable")

    response = _client(
        isolated_source_control_rw_engine,
        dependencies=dependencies,
        guard=guard,
        runtime_provider=unavailable_runtime,
    ).get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{MISSING_DELIVERY_ID}",
        headers={"X-Request-ID": "req-agentdeliveryunavailable"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "title": "Source Control agent delivery query unavailable",
        "status": 503,
        "requestId": "req-agentdeliveryunavailable",
    }


def test_default_agent_delivery_route_requires_session_and_has_safe_openapi_contract() -> None:
    app = create_app()
    path = "/api/v1/workspaces/{workspaceId}/agent-deliveries/{deliveryId}"
    operation = app.openapi()["paths"][path]["get"]

    response = TestClient(app, raise_server_exceptions=False).get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{MISSING_DELIVERY_ID}"
    )

    assert response.status_code == 401
    assert operation["security"] == [{"EpSessionCookie": []}]
    assert set(app.openapi()["paths"][path]) == {"get"}
    response_schema = app.openapi()["components"]["schemas"]["AgentDeliveryResponseDto"]
    serialized_schema = str(response_schema)
    for forbidden in (
        "grantDigest",
        "executionBindingDigest",
        "requestFingerprint",
        "fencingToken",
        "credential",
        "environment",
        "command",
    ):
        assert forbidden not in serialized_schema
