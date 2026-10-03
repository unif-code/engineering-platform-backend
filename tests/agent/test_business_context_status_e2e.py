from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
from control_plane.app.modules.agent.api import AgentHttpRuntime, routes
from control_plane.app.modules.agent.application.errors import (
    AgentBusinessContextReason,
    InvalidRequirementExecutionContext,
)
from control_plane.app.modules.agent.application.queries import AgentRunNotFound
from control_plane.app.modules.agent.domain import AgentQueryUnavailable
from control_plane.app.modules.requirement import (
    RequirementDependencyUnavailable,
    RequirementNotFound,
    acknowledge_repository_binding_request,
    claim_repository_binding_requests,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_business_context_status import context
from tests.agent.test_repository import (
    ATTEMPT,
    BINDING,
    DEFINITION,
    NOW,
    RUN,
    insert_predecessor_run,
)
from tests.agent.test_run_queries import seed_run
from tests.agent.test_run_query_e2e import agent_facts
from tests.agent.test_start_run import _dependencies
from tests.source_control.test_v06_production_e2e import Journey, _grant, _revoke, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database

PATH = f"/api/v1/agent-runs/{RUN.id}/business-context-status"


@pytest.fixture
def status_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    metadata = Mock(return_value=RUN)
    full = Mock(side_effect=AssertionError("status authorization must not load Attempt/Binding"))
    monkeypatch.setattr(routes, "get_run_metadata", metadata)
    monkeypatch.setattr(routes, "get_run", full)
    dependencies = replace(
        _dependencies(cast(IsolatedAgentDatabase, SimpleNamespace(runtime=Mock()))),
        clock=lambda: NOW,
    )
    port = Mock()
    port.resolve.return_value = context()
    factory = Mock(return_value=port)
    owner = MagicMock()
    connection = Mock()
    owner.connect.return_value.__enter__.return_value = connection
    runtime = AgentHttpRuntime(
        engine=Mock(),
        dependencies=dependencies,
        requirement_engine=owner,
        requirement_context_factory=factory,
    )
    guard = Mock()
    principal = SimpleNamespace(account_id="member")
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(routes.create_agent_router(lambda: runtime, lambda: principal, guard))
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            metadata=metadata,
            full=full,
            owner=owner,
            factory=factory,
            port=port,
            guard=guard,
            principal=principal,
            connection=connection,
        )


def test_current_projection_uses_recorded_scope_and_request_connection_without_etag(
    status_api: SimpleNamespace,
) -> None:
    result = status_api.client.get(PATH, params={"workspaceId": "client-cannot-override"})
    assert result.status_code == 200, result.text
    assert result.json() == {
        "runId": RUN.id,
        "workspaceId": RUN.workspace_id,
        "checkedAt": NOW.isoformat().replace("+00:00", "Z"),
        "currentness": "CURRENT",
        "reasons": [],
    }
    assert result.headers["cache-control"] == "no-store" and "etag" not in result.headers
    status_api.guard.assert_called_once_with(
        status_api.principal, "agent.run.read", RUN.workspace_id
    )
    status_api.factory.assert_called_once_with(status_api.connection)
    status_api.owner.connect.return_value.__exit__.assert_called_once()
    status_api.full.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_authorization_failure_never_opens_owner_connection(
    status_api: SimpleNamespace, status: int
) -> None:
    status_api.guard.side_effect = HTTPException(status, "Denied")
    result = status_api.client.get(PATH)
    assert result.status_code == status
    status_api.owner.connect.assert_not_called()
    status_api.factory.assert_not_called()
    status_api.port.resolve.assert_not_called()


def test_null_source_never_opens_owner_connection(status_api: SimpleNamespace) -> None:
    status_api.metadata.return_value = RUN.model_copy(update={"business_context": None})
    result = status_api.client.get(PATH)
    assert result.status_code == 200
    assert result.json()["currentness"] == "UNVERIFIABLE"
    assert result.json()["reasons"] == ["BUSINESS_CONTEXT_NOT_RECORDED"]
    status_api.owner.connect.assert_not_called()
    status_api.factory.assert_not_called()


@pytest.mark.parametrize(
    ("error", "status"),
    [(AgentRunNotFound("private"), 404), (AgentQueryUnavailable("private"), 503)],
)
def test_agent_read_failure_is_not_an_owner_projection(
    status_api: SimpleNamespace, error: Exception, status: int
) -> None:
    status_api.metadata.side_effect = error
    result = status_api.client.get(PATH)
    assert result.status_code == status
    assert "currentness" not in result.json() and "private" not in result.text
    status_api.owner.connect.assert_not_called()
    status_api.guard.assert_not_called()


@pytest.mark.parametrize("phase", ["connect", "factory", "resolve", "close"])
def test_owner_dependency_failure_is_explicit_unverifiable_without_raw_diagnostics(
    status_api: SimpleNamespace,
    phase: str,
) -> None:
    targets = {
        "connect": status_api.owner.connect,
        "factory": status_api.factory,
        "resolve": status_api.port.resolve,
        "close": status_api.owner.connect.return_value.__exit__,
    }
    targets[phase].side_effect = RuntimeError("PRIVATE_OWNER_DIAGNOSTIC")
    result = status_api.client.get(PATH)
    assert result.status_code == 200, result.text
    assert result.json()["currentness"] == "UNVERIFIABLE"
    assert result.json()["reasons"] == ["OWNER_UNAVAILABLE"]
    assert "PRIVATE_OWNER_DIAGNOSTIC" not in result.text
    assert result.headers["cache-control"] == "no-store" and "etag" not in result.headers


@pytest.mark.parametrize("phase", ["connect", "factory", "resolve", "close"])
@pytest.mark.parametrize("status", [401, 403])
def test_any_real_authorization_failure_is_not_folded_into_unverifiable(
    status_api: SimpleNamespace,
    phase: str,
    status: int,
) -> None:
    targets = {
        "connect": status_api.owner.connect,
        "factory": status_api.factory,
        "resolve": status_api.port.resolve,
        "close": status_api.owner.connect.return_value.__exit__,
    }
    targets[phase].side_effect = HTTPException(status, "Owner access denied")
    result = status_api.client.get(PATH)
    assert result.status_code == status
    assert "currentness" not in result.json()


def test_invalid_run_path_does_not_read_agent_or_owner(status_api: SimpleNamespace) -> None:
    result = status_api.client.get("/api/v1/agent-runs/not-a-uuid/business-context-status")
    assert result.status_code == 422
    status_api.metadata.assert_not_called()
    status_api.owner.connect.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_starlette_authorization_exceptions_remain_authorization_failures(
    status_api: SimpleNamespace,
    status: int,
) -> None:
    status_api.port.resolve.side_effect = StarletteHTTPException(status, "Denied")
    result = status_api.client.get(PATH)
    assert result.status_code == status and "currentness" not in result.json()


def test_openapi_adds_only_a_read_projection_with_exact_finite_reasons() -> None:
    schema = bootstrap.create_app().openapi()
    path = schema["paths"]["/api/v1/agent-runs/{runId}/business-context-status"]
    assert set(path) == {"get"}
    operation = path["get"]
    assert operation["operationId"] == "agent_run_business_context_status_get"
    assert "requestBody" not in operation
    assert [(param["name"], param["in"]) for param in operation["parameters"]] == [
        ("runId", "path")
    ]
    assert "ETag" not in operation["responses"]["200"]["headers"]
    result = schema["components"]["schemas"]["AgentRunBusinessContextStatusResponseDto"]
    assert set(result["required"]) == {
        "runId",
        "workspaceId",
        "checkedAt",
        "currentness",
        "reasons",
    }
    assert result["additionalProperties"] is False
    assert set(schema["components"]["schemas"]["AgentBusinessContextCurrentness"]["enum"]) == {
        "CURRENT",
        "STALE",
        "UNVERIFIABLE",
    }
    assert set(schema["components"]["schemas"]["AgentBusinessContextReason"]["enum"]) == {
        "BUSINESS_CONTEXT_NOT_RECORDED",
        "OWNER_UNAVAILABLE",
        "OWNER_DATA_INVALID",
        "OWNER_DATA_AMBIGUOUS",
        "REQUIREMENT_NOT_FOUND",
        "WORKSPACE_CHANGED",
        "WORK_ITEM_NOT_IN_REQUIREMENT",
        "ASSIGNMENT_MISSING",
        "ASSIGNMENT_CHANGED",
    }


def _evidence(journey: Journey) -> tuple[Any, Any, Any]:
    with journey.database.owner.connect() as db:
        keys = (
            db.execute(text("SELECT to_jsonb(t) FROM agent.idempotency_key t ORDER BY 1"))
            .scalars()
            .all()
        )
        audits = (
            db.execute(
                text(
                    "SELECT to_jsonb(t) FROM audit.audit_event t "
                    "WHERE action LIKE 'agent.%' OR action LIKE 'requirement.%' ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
    return agent_facts(journey), keys, audits


def _start_source_run(journey: Journey) -> tuple[dict[str, Any], Any]:
    created = _write(
        journey.member,
        "/api/v1/requirements",
        {
            "workspaceId": journey.workspace_id,
            "type": "feat",
            "title": "Association observation",
            "description": "Synthetic owner facts without external execution",
            "acceptanceCriteria": ["Preserve startup identities"],
            "initialRepositoryId": journey.repository_id,
        },
        status=201,
    ).json()
    for capability in ("agent.run.execute", "agent.run.read"):
        _grant(journey.admin, journey.member_id, capability, journey.workspace_id)
    body = {
        "workspaceId": journey.workspace_id,
        "requirementId": created["requirement"]["id"],
        "workItemId": created["workItem"]["id"],
        "definitionId": "00000000-0000-0000-0000-000000000800",
        "definitionVersion": 1,
        "goal": "Association is not current execution qualification",
    }
    return body, _write(
        journey.member, "/api/v1/agent-runs", body, status=202, key="association-start"
    )


@contextmanager
def _status_client(journey: Journey, runtime: AgentHttpRuntime) -> Iterator[TestClient]:
    with TestClient(
        bootstrap.create_app(agent_runtime_provider=lambda: runtime),
        base_url="https://testserver",
    ) as client:
        client.cookies.update(journey.member.cookies)
        yield client


@pytest.mark.integration
def test_default_session_association_changes_after_real_reassignment_without_agent_writes(
    journey: Journey,
) -> None:
    body, started = _start_source_run(journey)
    original = started.json()["run"]
    path = f"/api/v1/agent-runs/{original['id']}/business-context-status"
    runtime = bootstrap.agent_http_runtime()
    connections = []

    def owner_factory(db: Any) -> Any:
        connections.append(db)
        return runtime.requirement_context_factory(db)

    guarded = replace(runtime, requirement_context_factory=owner_factory)
    with _status_client(journey, guarded) as client:
        before = _evidence(journey)
        current = client.get(path)
        assert current.status_code == 200, current.text
        assert current.json()["currentness"] == "CURRENT" and current.json()["reasons"] == []
        assert (
            current.json()["runId"] == original["id"]
            and current.json()["workspaceId"] == journey.workspace_id
        )
        assert current.headers["cache-control"] == "no-store" and "etag" not in current.headers
        assert len(connections) == 1 and connections[0].closed
        assert _evidence(journey) == before

        dependencies = bootstrap.requirement_dependencies()
        now = dependencies.clock.now()
        with journey.database.engines["requirement"].begin() as db:
            messages = claim_repository_binding_requests(
                db,
                limit=1,
                available_before=now,
                lease_until=now + timedelta(minutes=1),
                dependencies=dependencies,
            )
            assert len(messages) == 1 and messages[0].requirement_id == body["requirementId"]
            prepared = acknowledge_repository_binding_request(
                db,
                message_id=messages[0].message_id,
                consumer="SOURCE_CONTROL",
                dependencies=dependencies,
            )
            assert prepared.state.value == "PREPARING"
        owner = journey.member.get(f"/api/v1/requirements/{body['requirementId']}").json()
        work = next(item for item in owner["workItems"] if item["id"] == body["workItemId"])
        changed = _write(
            journey.member,
            f"/api/v1/requirements/{body['requirementId']}/work-items/{body['workItemId']}:assign",
            {"humanOwnerId": journey.leader_id, "reason": "Reassign after startup"},
            etag=f'"v{work["revision"]}"',
        ).json()
        assert changed["assignment"]["id"] != original["businessContext"]["assignmentId"]
        before = _evidence(journey)
        stale = client.get(path)
        assert stale.status_code == 200 and stale.json()["currentness"] == "STALE"
        assert stale.json()["reasons"] == ["ASSIGNMENT_CHANGED"]
        assert changed["assignment"]["id"] not in stale.text
        assert len(connections) == 2 and all(connection.closed for connection in connections)
        assert client.get(f"/api/v1/agent-runs/{original['id']}").json()["run"] == original
        replay = _write(client, "/api/v1/agent-runs", body, status=202, key="association-start")
        assert (
            replay.content == started.content and replay.headers["etag"] == started.headers["etag"]
        )
        assert _evidence(journey) == before


@pytest.mark.integration
def test_default_session_denials_and_unrecorded_snapshot_never_call_owner(journey: Journey) -> None:
    _, started = _start_source_run(journey)
    with journey.database.engines["agent"].begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        foreign, _, _ = seed_run(repository, 900, str(UUID(int=999)))
        legacy = RUN.model_copy(
            update={"workspace_id": journey.workspace_id, "business_context": None}
        )
        insert_predecessor_run(db, legacy)
        repository.insert_attempt(ATTEMPT)
        repository.insert_binding(ATTEMPT.id, BINDING)
    runtime = bootstrap.agent_http_runtime()
    owner = Mock(side_effect=AssertionError("owner must not be called"))
    before = _evidence(journey)
    path = f"/api/v1/agent-runs/{started.json()['run']['id']}/business-context-status"
    with _status_client(journey, replace(runtime, requirement_context_factory=owner)) as client:
        client.cookies.clear()
        client.cookies.update(journey.leader.cookies)
        assert client.get(path).status_code == 403
        client.cookies.clear()
        client.cookies.update(journey.member.cookies)
        assert (
            client.get(f"/api/v1/agent-runs/{foreign.id}/business-context-status").status_code
            == 403
        )
        absent = client.get(f"/api/v1/agent-runs/{legacy.id}/business-context-status")
        assert absent.status_code == 200 and absent.json()["reasons"] == [
            "BUSINESS_CONTEXT_NOT_RECORDED"
        ]
        assert _evidence(journey) == before
        _revoke(journey, journey.member_id, "agent.run.read")
        assert client.get(path).status_code == 403
    owner.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("error", "state", "reason"),
    [
        (RequirementNotFound("private-owner-id"), "STALE", "REQUIREMENT_NOT_FOUND"),
        (
            InvalidRequirementExecutionContext(
                "private-owner", reason=AgentBusinessContextReason.OWNER_DATA_AMBIGUOUS
            ),
            "UNVERIFIABLE",
            "OWNER_DATA_AMBIGUOUS",
        ),
        (
            RequirementDependencyUnavailable("private-credentials-path"),
            "UNVERIFIABLE",
            "OWNER_UNAVAILABLE",
        ),
    ],
)
def test_default_session_owner_failure_is_a_read_only_bounded_projection(
    journey: Journey,
    error: Exception,
    state: str,
    reason: str,
) -> None:
    _, started = _start_source_run(journey)
    runtime = bootstrap.agent_http_runtime()
    port = Mock()
    port.resolve.side_effect = error
    before = _evidence(journey)
    with _status_client(
        journey, replace(runtime, requirement_context_factory=lambda _db: port)
    ) as client:
        response = client.get(
            f"/api/v1/agent-runs/{started.json()['run']['id']}/business-context-status"
        )
    assert response.status_code == 200
    assert (response.json()["currentness"], response.json()["reasons"]) == (state, [reason])
    assert "private" not in response.text
    assert _evidence(journey) == before
