from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.agent.api import routes
from control_plane.app.modules.agent.api.runtime import AgentHttpRuntime
from control_plane.app.modules.agent.application.queries import CanonicalEventPage
from tests.agent.test_repository import DEFINITION, EVENT, RUN
from tests.agent.test_run_queries import seed_run
from tests.source_control.test_v06_production_e2e import Journey, _grant, _revoke
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database


def test_default_contract_adds_only_the_workspace_get_operation() -> None:
    schema = bootstrap.create_app().openapi()
    path = schema["paths"]["/api/v1/agent-runs"]
    assert "get" in path, "Workspace Agent run directory is missing"
    assert path["get"]["operationId"] == "agent_runs_list"
    assert path["post"]["operationId"] == "agent_runs_start"
    parameters = {row["name"]: row for row in path["get"]["parameters"]}
    assert parameters["workspaceId"]["required"] is True
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 100
    assert parameters["cursor"]["schema"]["anyOf"][0]["maxLength"] == 2048


def test_directory_authorizes_workspace_before_obtaining_runtime_or_rows() -> None:
    runtime = Mock(side_effect=AssertionError("unauthorized directory must not be queried"))
    guard = Mock(side_effect=HTTPException(403, "Forbidden"))
    principal = SimpleNamespace(account_id="test-only")
    app = FastAPI()
    app.include_router(routes.create_agent_router(runtime, lambda: principal, guard))
    with TestClient(app) as client:
        result = client.get("/api/v1/agent-runs", params={"workspaceId": RUN.workspace_id})
    assert result.status_code == 403
    guard.assert_called_once_with(principal, "agent.run.read", RUN.workspace_id)
    runtime.assert_not_called()


def test_event_authorization_uses_actual_run_metadata_without_loading_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hasattr(routes, "get_run_metadata"), "event authorization still loads full details"
    metadata = Mock(return_value=RUN)
    full = Mock(side_effect=AssertionError("events must not load all attempts/bindings"))
    events = Mock(return_value=CanonicalEventPage(items=(), next_cursor=None))
    monkeypatch.setattr(routes, "get_run_metadata", metadata)
    monkeypatch.setattr(routes, "get_run", full)
    monkeypatch.setattr(routes, "list_events", events)
    guard = Mock()
    principal = SimpleNamespace(account_id="test-only")
    app = FastAPI()
    app.include_router(
        routes.create_agent_router(
            lambda: cast(AgentHttpRuntime, SimpleNamespace(dependencies=object())),
            lambda: principal,
            guard,
        )
    )
    with TestClient(app) as client:
        result = client.get(
            f"/api/v1/agent-runs/{RUN.id}/events",
            params={"workspaceId": str(UUID(int=999)), "limit": 1},
        )
    assert result.status_code == 200 and result.json() == {"items": [], "nextCursor": None}
    guard.assert_called_once_with(principal, "agent.run.read", RUN.workspace_id)
    full.assert_not_called()
    assert events.call_args.kwargs["limit"] == 1


def agent_facts(journey: Journey) -> list[Any]:
    with journey.database.owner.connect() as db:
        return [
            db.execute(text(f"SELECT to_jsonb(t) FROM agent.{name} t ORDER BY 1")).scalars().all()
            for name in (
                "agent_run",
                "agent_attempt",
                "execution_binding",
                "canonical_event",
                "workflow_command",
            )
        ]


def test_directory_dependency_failure_is_not_an_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from control_plane.app.modules.agent.domain import AgentQueryUnavailable

    monkeypatch.setattr(routes, "list_runs", Mock(side_effect=AgentQueryUnavailable("unavailable")))
    app = FastAPI()
    app.include_router(
        routes.create_agent_router(
            lambda: cast(AgentHttpRuntime, SimpleNamespace(dependencies=object())),
            lambda: SimpleNamespace(),
            lambda *_args: None,
        )
    )
    with TestClient(app) as client:
        result = client.get("/api/v1/agent-runs", params={"workspaceId": RUN.workspace_id})
    assert result.status_code == 503
    assert result.headers["content-type"].startswith("application/problem+json")
    assert "items" not in result.json()


@pytest.mark.integration
def test_default_session_directory_details_events_navigation_and_readonly_facts(
    journey: Journey,
) -> None:
    from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
    from control_plane.app.modules.agent.domain import ExecutionBindingSource

    with journey.database.engines["agent"].begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        first, attempt, _ = seed_run(repository, 300, journey.workspace_id)
        second, _, _ = seed_run(
            repository, 200, journey.workspace_id, source=ExecutionBindingSource.CONFIGURATION
        )
        foreign, _, _ = seed_run(repository, 400, str(UUID(int=999)))
        event = EVENT.model_copy(update={"attempt_id": attempt.id})
        repository.append_event(event)
    before = agent_facts(journey)
    _grant(journey.admin, journey.member_id, "agent.run.execute", journey.workspace_id)
    assert (
        journey.member.get(
            "/api/v1/agent-runs", params={"workspaceId": journey.workspace_id}
        ).status_code
        == 403
    )
    _grant(journey.admin, journey.member_id, "agent.run.read", journey.workspace_id)
    runtime = bootstrap.agent_http_runtime()
    external = Mock(side_effect=AssertionError("query cannot invoke execution services"))
    guarded = replace(
        runtime,
        dependencies=replace(
            runtime.dependencies,
            binding_policy=SimpleNamespace(resolve=external),
            workflow_orchestrator=SimpleNamespace(
                start=external, cancel=external, resume=external, lookup=external
            ),
        ),
    )
    with patch.object(bootstrap, "agent_http_runtime", return_value=guarded):
        with TestClient(bootstrap.create_app(), base_url="https://testserver") as client:
            client.cookies.update(journey.member.cookies)
            response = client.get(
                "/api/v1/agent-runs", params={"workspaceId": journey.workspace_id, "limit": 1}
            )
            assert response.status_code == 200
            page = response.json()
            assert page["items"][0]["run"]["id"] == first.id
            assert page["items"][0]["binding"]["source"] == "DEV_FAKE"
            assert page["items"][0]["latestAttempt"]["id"] == attempt.id
            following = client.get(
                "/api/v1/agent-runs",
                params={
                    "workspaceId": journey.workspace_id,
                    "limit": 1,
                    "cursor": page["nextCursor"],
                },
            ).json()
            assert following["items"][0]["run"]["id"] == second.id
            assert following["items"][0]["binding"]["source"] == "CONFIGURATION"
            assert following["nextCursor"] is None
            detail = client.get(f"/api/v1/agent-runs/{first.id}")
            assert detail.status_code == 200 and detail.headers["etag"] == '"v1"'
            events = client.get(f"/api/v1/agent-runs/{first.id}/events", params={"limit": 1})
            assert events.status_code == 200 and events.json()["items"][0]["id"] == event.id
            assert "data" not in events.json()["items"][0]
            assert "runtimePermissions" not in detail.text
            assert (
                client.get(
                    "/api/v1/agent-runs", params={"workspaceId": foreign.workspace_id}
                ).status_code
                == 403
            )
            assert client.get(f"/api/v1/agent-runs/{foreign.id}").status_code == 403
            assert (
                client.get(
                    f"/api/v1/agent-runs/{foreign.id}/events",
                    params={"workspaceId": journey.workspace_id},
                ).status_code
                == 403
            )
            assert client.get(
                "/api/v1/agent-runs",
                params={"workspaceId": journey.workspace_id, "state": "FAILED"},
            ).json() == {"items": [], "nextCursor": None}
            assert (
                client.get(
                    "/api/v1/agent-runs",
                    params={
                        "workspaceId": journey.workspace_id,
                        "state": "FAILED",
                        "cursor": page["nextCursor"],
                    },
                ).status_code
                == 422
            )
            assert (
                client.get(
                    "/api/v1/agent-runs", params={"workspaceId": journey.workspace_id, "limit": 101}
                ).status_code
                == 422
            )
            assert client.get("/api/v1/agent-runs").status_code == 422
            navigation = client.get("/api/v1/navigation").json()
            route = next(row for row in navigation if row["routeKey"] == "agent-runs")
            assert route["capability"] == "agent.run.read" and route["scopeType"] == "WORKSPACE"
            assert "actionCapabilities" not in route["meta"]
    external.assert_not_called()
    assert agent_facts(journey) == before
    _revoke(journey, journey.member_id, "agent.run.read")
    assert (
        journey.member.get(
            "/api/v1/agent-runs", params={"workspaceId": journey.workspace_id}
        ).status_code
        == 403
    )
    assert journey.member.get(f"/api/v1/agent-runs/{first.id}").status_code == 403
    assert journey.member.get(f"/api/v1/agent-runs/{first.id}/events").status_code == 403
    assert "agent-runs" not in {
        row["routeKey"] for row in journey.member.get("/api/v1/navigation").json()
    }
    with TestClient(bootstrap.create_app(), base_url="https://testserver") as anonymous:
        assert (
            anonymous.get(
                "/api/v1/agent-runs", params={"workspaceId": journey.workspace_id}
            ).status_code
            == 401
        )
    with journey.database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE identity.session SET created_at=now()-interval '2 days', "
                "last_seen_at=now()-interval '2 days', expires_hint=now()-interval '1 day' "
                "WHERE account_id=CAST(:id AS UUID)"
            ),
            {"id": journey.member_id},
        )
    assert (
        journey.member.get(
            "/api/v1/agent-runs", params={"workspaceId": journey.workspace_id}
        ).status_code
        == 401
    )


@pytest.mark.integration
def test_agent_run_route_registration_does_not_overwrite_operator_configuration(
    journey: Journey,
) -> None:
    import importlib
    from unittest.mock import patch as mock_patch

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import DBAPIError

    migration = importlib.import_module("migrations.authorization.0011_authorization_agent_queries")
    with journey.database.owner.connect() as db:
        grants = db.execute(text('SELECT count(*) FROM "authorization"."grant"')).scalar_one()
    with journey.database.owner.begin() as db:
        db.execute(
            text(
                'UPDATE "authorization".route_registry '
                "SET meta='{\"name\":\"Operator custom\"}' WHERE route_key='agent-runs'"
            )
        )
    try:
        with (
            pytest.raises(DBAPIError, match="conflicting managed route"),
            journey.database.owner.begin() as db,
        ):
            with mock_patch.object(migration, "op", Operations(MigrationContext.configure(db))):
                migration.upgrade()
        with journey.database.owner.connect() as db:
            assert db.execute(
                text(
                    "SELECT meta FROM \"authorization\".route_registry WHERE route_key='agent-runs'"
                )
            ).scalar_one() == {"name": "Operator custom"}
            assert (
                db.execute(text('SELECT count(*) FROM "authorization"."grant"')).scalar_one()
                == grants
            )
    finally:
        with journey.database.owner.begin() as db:
            db.execute(
                text(
                    'UPDATE "authorization".route_registry '
                    'SET meta=\'{"name":"Agent 运行记录","order":21}\' '
                    "WHERE route_key='agent-runs'"
                )
            )
