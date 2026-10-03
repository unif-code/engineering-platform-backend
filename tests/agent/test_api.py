from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.agent import accept_workflow_event
from control_plane.app.modules.agent.adapters import (
    DevActorResolver,
    DevDefinitionAvailabilityPolicy,
    DevEventCursorCodec,
    DevExecutionBindingPolicy,
    SqlAlchemyAgentTransactionRunner,
)
from control_plane.app.modules.agent.adapters.dev_temporal import DevTemporalAdapter
from control_plane.app.modules.agent.api import AgentHttpRuntime, create_agent_router
from control_plane.app.modules.agent.api.routes import _problem
from control_plane.app.modules.agent.api.runtime import (
    CurrentPrincipalActorResolver,
    UnboundRequirementExecutionContext,
)
from control_plane.app.modules.agent.application.control import AttemptRevisionConflict
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.errors import InvalidRequirementExecutionContext
from control_plane.app.modules.agent.application.idempotency import (
    AgentReplayUnavailable,
    IdempotencyConflict,
    IdempotencyInProgress,
)
from control_plane.app.modules.agent.domain import CanonicalEventInput
from control_plane.app.modules.agent.ports.repository import AgentTransactionRunner
from control_plane.app.modules.agent.ports.runtime import (
    RequirementExecutionContext,
    RequirementExecutionRequest,
)
from control_plane.app.modules.authorization import (
    AuthorizationPrincipal,
    DecisionDependencies,
    Scope,
    grant,
    principal_has_capability,
    resolve_principal,
    revoke,
)
from control_plane.app.modules.identity import SessionKind, SessionPrincipal
from control_plane.app.modules.requirement import (
    RequirementDependencyUnavailable,
    RequirementNotFound,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from tests.agent.conftest import IsolatedAgentDatabase, TestSecretManager
from tests.authorization.helpers import authorization_dependencies

WORKSPACE_ID = "00000000-0000-0000-0000-000000000801"
REQUIREMENT_ID = "00000000-0000-0000-0000-000000000802"
WORK_ITEM_ID = "00000000-0000-0000-0000-000000000803"
DEFINITION_ID = "00000000-0000-0000-0000-000000000800"
NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)

START_BODY = {
    "workspaceId": WORKSPACE_ID,
    "requirementId": REQUIREMENT_ID,
    "workItemId": WORK_ITEM_ID,
    "definitionId": DEFINITION_ID,
    "definitionVersion": 1,
    "goal": "Prove the governed control-plane path without side effects",
}


@pytest.mark.parametrize(
    ("error_type", "status", "code"),
    [
        (AttemptRevisionConflict, 409, "ATTEMPT_REVISION_CONFLICT"),
        (IdempotencyConflict, 409, "IDEMPOTENCY_CONFLICT"),
        (IdempotencyInProgress, 409, "IDEMPOTENCY_IN_PROGRESS"),
        (AgentReplayUnavailable, 503, "AGENT_REPLAY_UNAVAILABLE"),
    ],
)
def test_command_problem_codes_distinguish_rejection_from_unknown_outcome(
    error_type: type[Exception], status: int, code: str
) -> None:
    response = _problem(error_type("PRIVATE_DIAGNOSTIC"))
    payload = bytes(response.body)
    body = json.loads(payload)
    assert response.status_code == body["status"] == status
    assert body["title"] == (
        "Agent state conflict" if status == 409 else "Agent service unavailable"
    )
    assert body["code"] == code
    assert "PRIVATE_DIAGNOSTIC" not in payload.decode()
    assert response.headers["content-type"].startswith("application/problem+json")


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


class ConnectionBackedRequirementContext:
    def __init__(self, db: Connection) -> None:
        self.db = db

    @contextmanager
    def protect(
        self, request: RequirementExecutionRequest, *, expected_assignment_id: str
    ) -> Iterator[RequirementExecutionContext]:
        with self.db.begin():
            yield self.resolve(request)

    def resolve(self, request: RequirementExecutionRequest) -> RequirementExecutionContext:
        assert self.db.execute(text("SELECT 1")).scalar_one() == 1
        return RequirementExecutionContext(
            workspace_id=request.workspace_id,
            requirement_id=request.requirement_id,
            work_item_id=request.work_item_id,
            assignment_id="00000000-0000-0000-0000-000000000804",
            goal_ref=f"requirement:{request.requirement_id}:work-item:{request.work_item_id}",
        )


@dataclass(slots=True)
class MutableAuthorization:
    principal: AuthorizationPrincipal
    allowed: set[tuple[str, str | None]]
    principal_resolutions: int = 0
    unavailable: bool = False

    def resolve(self, request: Request) -> AuthorizationPrincipal:
        self.principal_resolutions += 1
        if request.cookies.get("ep_session") != "session-current":
            raise HTTPException(status_code=401, detail="Authentication required")
        return self.principal

    def guard(self, _principal: Any, capability: str, workspace_id: str | None) -> None:
        if self.unavailable:
            raise HTTPException(status_code=503, detail="Authorization unavailable")
        if (capability, workspace_id) not in self.allowed:
            raise HTTPException(status_code=403, detail="Forbidden")


@dataclass(frozen=True, slots=True)
class AgentApiHarness:
    database: IsolatedAgentDatabase
    runtime: AgentHttpRuntime
    client: TestClient
    authorization: MutableAuthorization
    clock: MutableClock
    requirement_connections: list[Connection]
    runtime_provider: CountingRuntimeProvider


@dataclass(slots=True)
class CountingRuntimeProvider:
    runtime: AgentHttpRuntime
    calls: int = 0

    def __call__(self) -> AgentHttpRuntime:
        self.calls += 1
        return self.runtime


def _dependencies(database: IsolatedAgentDatabase, clock: MutableClock) -> AgentDependencies:
    class UnboundRequirementContext(UnboundRequirementExecutionContext):
        def resolve(self, _request: RequirementExecutionRequest) -> RequirementExecutionContext:
            raise AssertionError("HTTP start must bind a request-scoped Requirement context")

    return AgentDependencies(
        transaction_runner=SqlAlchemyAgentTransactionRunner(database.runtime),
        requirement_context=UnboundRequirementContext(),
        binding_policy=DevExecutionBindingPolicy(),
        definition_availability=DevDefinitionAvailabilityPolicy(),
        actor_resolver=DevActorResolver(),
        clock=clock,
        new_id=lambda: str(uuid4()),
        cursor_codec=DevEventCursorCodec(),
        secret_manager=TestSecretManager(),
        workflow_orchestrator=DevTemporalAdapter(),
    )


def _client_for(
    runtime: AgentHttpRuntime,
    authorization: MutableAuthorization,
    runtime_provider: CountingRuntimeProvider | None = None,
) -> TestClient:
    provider = runtime_provider or CountingRuntimeProvider(runtime)
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_agent_router(
            provider,
            cast(Callable[[], Any], authorization.resolve),
            authorization.guard,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    client.cookies.set("ep_session", "session-current")
    return client


@pytest.fixture
def agent_api(isolated_agent_database: IsolatedAgentDatabase) -> AgentApiHarness:
    clock = MutableClock()
    connections: list[Connection] = []

    def requirement_context_factory(db: Connection) -> ConnectionBackedRequirementContext:
        connections.append(db)
        return ConnectionBackedRequirementContext(db)

    runtime = AgentHttpRuntime(
        engine=isolated_agent_database.runtime,
        dependencies=_dependencies(isolated_agent_database, clock),
        requirement_engine=isolated_agent_database.owner,
        requirement_context_factory=requirement_context_factory,
    )
    authorization = MutableAuthorization(
        principal=AuthorizationPrincipal(
            account_id="account-http-901",
            employee_id="employee-http-901",
            name="HTTP Agent User",
            is_super_admin=False,
            authorization_version=1,
            capabilities=(),
        ),
        allowed={
            ("agent.definition.read", None),
            ("agent.run.execute", WORKSPACE_ID),
            ("agent.run.read", WORKSPACE_ID),
            ("agent.run.control", WORKSPACE_ID),
        },
    )
    runtime_provider = CountingRuntimeProvider(runtime)
    return AgentApiHarness(
        database=isolated_agent_database,
        runtime=runtime,
        client=_client_for(runtime, authorization, runtime_provider),
        authorization=authorization,
        clock=clock,
        requirement_connections=connections,
        runtime_provider=runtime_provider,
    )


def _write_headers(key: str, *, etag: str | None = None) -> dict[str, str]:
    headers = {
        "Idempotency-Key": key,
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "X-Request-ID": f"req-{key.lower()}",
    }
    if etag is not None:
        headers["If-Match"] = etag
    return headers


def _start(agent: AgentApiHarness, *, key: str = "agent-start-901") -> Any:
    response = agent.client.post(
        "/api/v1/agent-runs",
        json=START_BODY,
        headers=_write_headers(key),
    )
    assert response.status_code == 202, response.text
    return response


def _event(
    *,
    attempt_id: str,
    generation: int,
    sequence: int,
    event_type: str,
    data: dict[str, object] | None = None,
) -> CanonicalEventInput:
    return CanonicalEventInput(
        id=str(uuid4()),
        event_type=event_type,
        attempt_id=attempt_id,
        generation=generation,
        sequence=sequence,
        correlation_id=f"worker-correlation-{sequence}",
        causation_id=None,
        trace_id=f"worker-trace-{sequence}",
        span_id=f"worker-span-{sequence}",
        summary=f"safe {event_type} summary",
        data=data or {},
    )


def _advance_to_waiting(agent: AgentApiHarness, attempt_id: str) -> int:
    for sequence, event_type in ((2, "ATTEMPT_PROVISIONING"), (3, "ATTEMPT_RUNNING")):
        accept_workflow_event(
            None,
            event=_event(
                attempt_id=attempt_id,
                generation=1,
                sequence=sequence,
                event_type=event_type,
            ),
            dependencies=agent.runtime.dependencies,
        )
    accepted = accept_workflow_event(
        None,
        event=_event(
            attempt_id=attempt_id,
            generation=1,
            sequence=4,
            event_type="WAITING_INPUT",
            data={
                "checkpoint": {
                    "id": str(uuid4()),
                    "artifact_id": "artifact-agent-901",
                    "artifact_version": "1",
                    "content_sha256": "sha256:" + "a" * 64,
                    "schema_version": "1",
                    "adapter_version": "dev-1",
                    "classification": "INTERNAL",
                },
                "waitingDeadline": (agent.clock.value + timedelta(minutes=15)).isoformat(),
                "question": {"prompt": "请确认本次 API 测试输入。"},
            },
        ),
        dependencies=agent.runtime.dependencies,
    )
    return accepted.attempt.revision


def test_agent_routes_return_strict_public_camel_case_contract(
    agent_api: AgentApiHarness,
) -> None:
    definitions = agent_api.client.get("/api/v1/agent-definitions")
    assert definitions.status_code == 200
    definition = definitions.json()["items"][0]
    assert definition["id"] == DEFINITION_ID
    assert "capabilityDeclarations" in definition
    assert "capability_declarations" not in definition

    started_response = _start(agent_api)
    started = started_response.json()
    assert set(started) == {"run", "attempt", "binding"}
    assert started["run"]["workspaceId"] == WORKSPACE_ID
    assert started["run"]["createdBy"] == "employee-http-901"
    assert set(started["binding"]) == {"id", "source", "digest"}
    forbidden_attempt_fields = {
        "fencingToken",
        "bindingDigest",
        "eventSequence",
        "terminalEvidence",
    }
    assert forbidden_attempt_fields.isdisjoint(started["attempt"])
    assert started_response.headers["etag"] == f'"v{started["attempt"]["revision"]}"'

    run_id = started["run"]["id"]
    details = agent_api.client.get(f"/api/v1/agent-runs/{run_id}")
    assert details.status_code == 200
    assert set(details.json()) == {"run", "attempts", "bindings", "waitingInput"}
    assert details.headers["etag"] == f'"v{details.json()["attempts"][-1]["revision"]}"'

    events = agent_api.client.get(f"/api/v1/agent-runs/{run_id}/events")
    assert events.status_code == 200
    assert set(events.json()) == {"items", "nextCursor"}
    assert "data" not in events.json()["items"][0]
    assert "command" not in json.dumps(started).lower()
    assert "receipt" not in json.dumps(started).lower()


def test_every_action_resolves_current_principal_and_rechecks_current_capability(
    agent_api: AgentApiHarness,
) -> None:
    started = _start(agent_api).json()
    run_id = started["run"]["id"]
    initial_resolutions = agent_api.authorization.principal_resolutions

    agent_api.authorization.allowed.remove(("agent.run.read", WORKSPACE_ID))
    denied = agent_api.client.get(f"/api/v1/agent-runs/{run_id}")
    assert denied.status_code == 403
    agent_api.authorization.allowed.add(("agent.run.read", WORKSPACE_ID))
    allowed = agent_api.client.get(f"/api/v1/agent-runs/{run_id}")
    assert allowed.status_code == 200
    assert agent_api.authorization.principal_resolutions == initial_resolutions + 2

    agent_api.authorization.allowed.remove(("agent.run.read", WORKSPACE_ID))
    agent_api.authorization.allowed.add(("agent.run.read", "00000000-0000-0000-0000-000000009999"))
    assert agent_api.client.get(f"/api/v1/agent-runs/{run_id}/events").status_code == 403


def test_http_actor_is_the_fresh_principal_and_cannot_be_supplied_by_the_client(
    agent_api: AgentApiHarness,
) -> None:
    injected = agent_api.client.post(
        "/api/v1/agent-runs",
        json={**START_BODY, "actor": "service-901"},
        headers=_write_headers("actor-injection-901"),
    )
    assert injected.status_code == 422
    assert "service-901" not in injected.text

    started = _start(agent_api, key="principal-actor-901").json()
    with agent_api.database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT actor, actor_type FROM audit.audit_event "
                "WHERE action='agent.run.start' AND target_id=:run_id"
            ),
            {"run_id": started["run"]["id"]},
        ).one()
    assert row == ("employee-http-901", "EMPLOYEE")


def test_start_preflight_rejects_missing_or_cross_origin_headers_before_mutation(
    agent_api: AgentApiHarness,
) -> None:
    assert agent_api.authorization.principal_resolutions == 0
    assert agent_api.runtime_provider.calls == 0
    missing_key = agent_api.client.post("/api/v1/agent-runs", json=START_BODY)
    assert missing_key.status_code == 422
    cross_origin = agent_api.client.post(
        "/api/v1/agent-runs",
        json=START_BODY,
        headers={
            "Idempotency-Key": "cross-origin-901",
            "Origin": "https://attacker.invalid",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_origin.status_code == 403
    assert agent_api.authorization.principal_resolutions == 0
    assert agent_api.runtime_provider.calls == 0
    assert agent_api.requirement_connections == []
    with agent_api.database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.agent_run")).scalar_one() == 0


def test_start_exact_replay_is_sealed_and_changed_request_conflicts(
    agent_api: AgentApiHarness,
) -> None:
    first = _start(agent_api, key="start-replay-901")
    second = _start(agent_api, key="start-replay-901")
    assert first.json() == second.json()
    assert first.headers["etag"] == second.headers["etag"]
    changed = agent_api.client.post(
        "/api/v1/agent-runs",
        json={**START_BODY, "goal": "different governed goal"},
        headers=_write_headers("start-replay-901"),
    )
    assert changed.status_code == 409
    assert changed.headers["content-type"].startswith("application/problem+json")
    assert changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert "different governed goal" not in changed.text


def test_http_original_body_and_etag_survive_resume_cancel_and_terminal_advancement(
    agent_api: AgentApiHarness,
) -> None:
    from tests.agent.test_final_integrity import facts

    agent = agent_api
    original_start = _start(agent)
    start_body = original_start.json()
    run_id, attempt_id = start_body["run"]["id"], start_body["attempt"]["id"]
    revision = _advance_to_waiting(agent, attempt_id)
    resume_path = f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/resume"
    resume_headers = _write_headers("advance-resume-901", etag=f'"v{revision}"')
    original_resume = agent.client.post(resume_path, json={}, headers=resume_headers)
    assert original_resume.status_code == 202
    cancel_path = f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/cancel"
    cancel_headers = _write_headers("advance-cancel-901", etag=original_resume.headers["etag"])
    original_cancel = agent.client.post(cancel_path, json={}, headers=cancel_headers)
    assert original_cancel.status_code == 202
    accept_workflow_event(
        None,
        event=_event(
            attempt_id=attempt_id, generation=2, sequence=1, event_type="ATTEMPT_CANCELED"
        ),
        dependencies=agent.runtime.dependencies,
    )
    before = facts(agent.database)
    replayed = [
        (_start(agent), original_start),
        (agent.client.post(resume_path, json={}, headers=resume_headers), original_resume),
        (agent.client.post(cancel_path, json={}, headers=cancel_headers), original_cancel),
    ]
    for replay, original in replayed:
        assert replay.status_code == original.status_code == 202
        assert replay.content == original.content
        assert replay.headers["etag"] == original.headers["etag"]
    assert facts(agent.database) == before
    changed = agent.client.post(
        resume_path,
        json={},
        headers=_write_headers("advance-resume-901", etag=original_resume.headers["etag"]),
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    agent.authorization.allowed.remove(("agent.run.control", WORKSPACE_ID))
    denied = agent.client.post(resume_path, json={}, headers=resume_headers)
    assert denied.status_code == 403
    assert facts(agent.database) == before


def test_http_unavailable_sealing_key_is_sanitized_and_rolls_back(
    agent_api: AgentApiHarness,
) -> None:
    from control_plane.app.shared.security import SecretMaterial, SecretMaterialUnavailable
    from tests.agent.test_final_integrity import facts

    class UnavailableSecrets:
        def load(self) -> SecretMaterial:
            raise SecretMaterialUnavailable("sensitive-sealing-detail")

    agent = agent_api
    agent.runtime_provider.runtime = replace(
        agent.runtime,
        dependencies=replace(agent.runtime.dependencies, secret_manager=UnavailableSecrets()),
    )
    before = facts(agent.database)
    response = agent.client.post(
        "/api/v1/agent-runs", json=START_BODY, headers=_write_headers("secret-unavailable-901")
    )
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "AGENT_REPLAY_UNAVAILABLE"
    assert "sensitive-sealing-detail" not in response.text
    assert facts(agent.database) == before


def test_cancel_requires_both_headers_and_replays_with_current_etag(
    agent_api: AgentApiHarness,
) -> None:
    started = _start(agent_api).json()
    run_id = started["run"]["id"]
    attempt = started["attempt"]
    path = f"/api/v1/agent-runs/{run_id}/attempts/{attempt['id']}/cancel"

    principal_resolutions = agent_api.authorization.principal_resolutions
    runtime_resolutions = agent_api.runtime_provider.calls
    assert agent_api.client.post(path).status_code == 422
    missing_match = agent_api.client.post(path, headers=_write_headers("cancel-missing-901"))
    assert missing_match.status_code == 422
    invalid_key = agent_api.client.post(
        path,
        headers={
            **_write_headers("cancel-invalid-901", etag=f'"v{attempt["revision"]}"'),
            "Idempotency-Key": "bad",
        },
    )
    assert invalid_key.status_code == 422
    cross_origin = agent_api.client.post(
        path,
        headers={
            **_write_headers("cancel-origin-901", etag=f'"v{attempt["revision"]}"'),
            "Origin": "https://attacker.invalid",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_origin.status_code == 403
    assert agent_api.authorization.principal_resolutions == principal_resolutions
    assert agent_api.runtime_provider.calls == runtime_resolutions
    with agent_api.database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT revision FROM agent.agent_attempt WHERE id=CAST(:id AS UUID)"),
                {"id": attempt["id"]},
            ).scalar_one()
            == attempt["revision"]
        )

    headers = _write_headers("cancel-replay-901", etag=f'"v{attempt["revision"]}"')
    first = agent_api.client.post(path, headers=headers)
    second = agent_api.client.post(path, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert first.headers["etag"] == second.headers["etag"]
    assert first.headers["etag"] == f'"v{first.json()["attempt"]["revision"]}"'
    assert set(first.json()) == {"attempt"}

    conflict = agent_api.client.post(
        path,
        headers=_write_headers("cancel-replay-901", etag=first.headers["etag"]),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_resume_keeps_binding_and_returns_next_generation_etag(
    agent_api: AgentApiHarness,
) -> None:
    started = _start(agent_api).json()
    run_id = started["run"]["id"]
    attempt_id = started["attempt"]["id"]
    waiting_revision = _advance_to_waiting(agent_api, attempt_id)
    response = agent_api.client.post(
        f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/resume",
        headers=_write_headers("resume-agent-901", etag=f'"v{waiting_revision}"'),
    )
    assert response.status_code == 202
    resumed = response.json()["attempt"]
    assert resumed["bindingId"] == started["attempt"]["bindingId"]
    assert resumed["runnerGeneration"] == 2
    assert response.headers["etag"] == f'"v{resumed["revision"]}"'
    replay = agent_api.client.post(
        f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/resume",
        headers=_write_headers("resume-agent-901", etag=f'"v{waiting_revision}"'),
    )
    assert replay.status_code == 202
    assert replay.json() == response.json()
    assert replay.headers["etag"] == response.headers["etag"]


def test_events_use_run_scoped_opaque_cursor_and_reject_tampering(
    agent_api: AgentApiHarness,
) -> None:
    started = _start(agent_api).json()
    run_id = started["run"]["id"]
    attempt_id = started["attempt"]["id"]
    accept_workflow_event(
        None,
        event=_event(
            attempt_id=attempt_id,
            generation=1,
            sequence=2,
            event_type="ATTEMPT_PROVISIONING",
        ),
        dependencies=agent_api.runtime.dependencies,
    )

    first = agent_api.client.get(f"/api/v1/agent-runs/{run_id}/events?limit=1")
    assert first.status_code == 200
    cursor = first.json()["nextCursor"]
    assert cursor and cursor.startswith("v1.")
    assert run_id not in cursor
    second = agent_api.client.get(
        f"/api/v1/agent-runs/{run_id}/events", params={"limit": 1, "cursor": cursor}
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["sequence"] == 2
    assert "must-never-cross-http" not in second.text
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    rejected = agent_api.client.get(
        f"/api/v1/agent-runs/{run_id}/events", params={"cursor": tampered}
    )
    assert rejected.status_code == 422


def test_problem_mapping_distinguishes_not_found_conflict_validation_and_unavailable(
    agent_api: AgentApiHarness,
) -> None:
    missing = agent_api.client.get(f"/api/v1/agent-runs/{uuid4()}")
    assert missing.status_code == 404

    started = _start(agent_api).json()
    run_id = started["run"]["id"]
    attempt = started["attempt"]
    missing_attempt = agent_api.client.post(
        f"/api/v1/agent-runs/{run_id}/attempts/{uuid4()}/cancel",
        headers=_write_headers(
            "missing-attempt-901",
            etag=f'"v{attempt["revision"]}"',
        ),
    )
    assert missing_attempt.status_code == 404
    illegal_resume = agent_api.client.post(
        f"/api/v1/agent-runs/{run_id}/attempts/{attempt['id']}/resume",
        headers=_write_headers("illegal-resume-901", etag=f'"v{attempt["revision"]}"'),
    )
    assert illegal_resume.status_code == 409
    invalid_match = agent_api.client.post(
        f"/api/v1/agent-runs/{run_id}/attempts/{attempt['id']}/cancel",
        headers=_write_headers("invalid-match-901", etag="weak-or-invalid"),
    )
    assert invalid_match.status_code == 422

    agent_api.authorization.unavailable = True
    unavailable_auth = agent_api.client.get(f"/api/v1/agent-runs/{run_id}")
    assert unavailable_auth.status_code == 503
    assert "employee-http-901" not in unavailable_auth.text

    def unavailable_runner(_operation: Any) -> Any:
        raise SQLAlchemyError("SENSITIVE_DATABASE_SENTINEL")

    unavailable_runtime = replace(
        agent_api.runtime,
        dependencies=replace(
            agent_api.runtime.dependencies,
            transaction_runner=cast(AgentTransactionRunner, unavailable_runner),
        ),
    )
    agent_api.authorization.unavailable = False
    unavailable_db = _client_for(unavailable_runtime, agent_api.authorization).get(
        "/api/v1/agent-definitions"
    )
    assert unavailable_db.status_code == 503
    assert "SENSITIVE_DATABASE_SENTINEL" not in unavailable_db.text

    anonymous = TestClient(agent_api.client.app, base_url="https://testserver")
    assert anonymous.get("/api/v1/agent-definitions").status_code == 401


def test_requirement_connection_is_request_scoped_for_complete_start_call(
    agent_api: AgentApiHarness,
) -> None:
    _start(agent_api, key="connection-scope-901")
    assert len(agent_api.requirement_connections) == 1
    assert agent_api.requirement_connections[0].closed


def test_agent_openapi_declares_exact_operations_cookie_security_and_problem_contract() -> None:
    schema = create_app().openapi()
    operations = {
        ("/api/v1/agent-definitions", "get"): "agent_definitions_list",
        ("/api/v1/agent-runs", "post"): "agent_runs_start",
        ("/api/v1/agent-runs", "get"): "agent_runs_list",
        ("/api/v1/agent-runs/{runId}", "get"): "agent_runs_get",
        ("/api/v1/agent-runs/{runId}/events", "get"): "agent_run_events_list",
        (
            "/api/v1/agent-runs/{runId}/business-context-status",
            "get",
        ): "agent_run_business_context_status_get",
        (
            "/api/v1/agent-runs/{runId}/attempts/{attemptId}/cancel",
            "post",
        ): "agent_attempt_cancel",
        (
            "/api/v1/agent-runs/{runId}/attempts/{attemptId}/resume",
            "post",
        ): "agent_attempt_resume",
    }
    for (path, method), operation_id in operations.items():
        operation = schema["paths"][path][method]
        assert operation["operationId"] == operation_id
        assert operation["security"] == [{"EpSessionCookie": []}]
        success = "202" if method == "post" else "200"
        assert success in operation["responses"]
        if operation_id in {
            "agent_runs_start",
            "agent_runs_get",
            "agent_attempt_cancel",
            "agent_attempt_resume",
        }:
            assert "ETag" in operation["responses"][success]["headers"]
        for status in ("401", "403", "404", "409", "422", "503"):
            assert "application/problem+json" in operation["responses"][status]["content"]

    start_schema = schema["components"]["schemas"]["StartAgentRunRequestDto"]
    assert start_schema["additionalProperties"] is False
    assert "actor" not in start_schema["properties"]
    control_schema = schema["components"]["schemas"]["AttemptControlRequestDto"]
    assert control_schema["additionalProperties"] is False
    assert control_schema["properties"] == {}
    for dto_name, field_name in (
        ("AgentRunResponseDto", "revision"),
        ("AgentAttemptResponseDto", "runnerGeneration"),
        ("AgentDefinitionResponseDto", "version"),
        ("CanonicalEventSummaryResponseDto", "sequence"),
    ):
        assert schema["components"]["schemas"][dto_name]["properties"][field_name]["minimum"] == 1
    assert (
        schema["components"]["schemas"]["AgentRunResponseDto"]["properties"]["goalRef"]["maxLength"]
        == 2048
    )
    for dto_name in (
        "AgentDefinitionResponseDto",
        "AgentDefinitionListResponseDto",
        "AgentRunResponseDto",
        "CheckpointSummaryResponseDto",
        "AgentAttemptResponseDto",
        "ExecutionBindingSummaryResponseDto",
        "StartAgentRunResponseDto",
        "AgentRunDetailsResponseDto",
        "CanonicalEventSummaryResponseDto",
        "CanonicalEventPageResponseDto",
        "AttemptControlResponseDto",
    ):
        assert schema["components"]["schemas"][dto_name]["additionalProperties"] is False
    serialized = json.dumps(
        {
            "paths": {path: schema["paths"][path] for path, _method in operations},
            "schemas": schema["components"]["schemas"],
        }
    ).casefold()
    for forbidden in (
        "temporal",
        "pydanticai",
        "workflowexecution",
        "openbao",
        "kata",
        "sandbox",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("operation", ["cancel", "resume"])
def test_controls_reject_extra_body_fields_without_agent_mutation(
    agent_api: AgentApiHarness, operation: str
) -> None:
    started = _start(agent_api).json()
    attempt = started["attempt"]
    response = agent_api.client.post(
        f"/api/v1/agent-runs/{started['run']['id']}/attempts/{attempt['id']}/{operation}",
        headers=_write_headers(f"extra-body-{operation}", etag=f'"v{attempt["revision"]}"'),
        json={"actor": "SECRET_ACTOR_SENTINEL"},
    )
    assert response.status_code == 422
    assert "SECRET_ACTOR_SENTINEL" not in response.text
    with agent_api.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT revision FROM agent.agent_attempt")).scalar_one()
            == attempt["revision"]
        )


@pytest.mark.parametrize("operation", ["definitions", "start", "get", "events", "cancel", "resume"])
def test_runtime_construction_failure_is_sanitized_service_unavailable(
    agent_api: AgentApiHarness, operation: str
) -> None:
    def unavailable_provider() -> AgentHttpRuntime:
        raise SQLAlchemyError("RUNTIME_DATABASE_SECRET_SENTINEL")

    client = _client_for(
        agent_api.runtime,
        agent_api.authorization,
        cast(CountingRuntimeProvider, unavailable_provider),
    )
    run_id, attempt_id = str(uuid4()), str(uuid4())
    paths = {
        "definitions": "/api/v1/agent-definitions",
        "start": "/api/v1/agent-runs",
        "get": f"/api/v1/agent-runs/{run_id}",
        "events": f"/api/v1/agent-runs/{run_id}/events",
        "cancel": f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/cancel",
        "resume": f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/resume",
    }
    response = client.request(
        "POST" if operation in {"start", "cancel", "resume"} else "GET",
        paths[operation],
        headers=_write_headers(f"runtime-failure-{operation}", etag='"v1"'),
        json=START_BODY if operation == "start" else None,
    )
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "RUNTIME_DATABASE_SECRET_SENTINEL" not in response.text


@pytest.mark.parametrize("operation", ["start", "cancel", "resume"])
def test_write_preflight_rejects_invalid_headers_before_all_dependencies(
    agent_api: AgentApiHarness, operation: str
) -> None:
    path = "/api/v1/agent-runs"
    if operation != "start":
        path += f"/{uuid4()}/attempts/{uuid4()}/{operation}"
    valid = _write_headers(f"preflight-{operation}", etag='"v1"')
    invalid = [
        ({key: value for key, value in valid.items() if key != "Idempotency-Key"}, 422),
        ({**valid, "Idempotency-Key": "invalid"}, 422),
        ({**valid, "Origin": "null"}, 403),
        ({**valid, "Origin": "https://attacker.invalid"}, 403),
        ({**valid, "Sec-Fetch-Site": "cross-site"}, 403),
    ]
    if operation != "start":
        invalid.extend(
            [
                ({key: value for key, value in valid.items() if key != "If-Match"}, 422),
                ({**valid, "If-Match": 'W/"v1"'}, 422),
                ({**valid, "If-Match": '"v1", "v2"'}, 422),
                ({**valid, "If-Match": "*"}, 422),
            ]
        )
    for headers, status in invalid:
        response = agent_api.client.post(
            path, headers=headers, json=START_BODY if operation == "start" else None
        )
        assert response.status_code == status
        assert response.headers["content-type"].startswith("application/problem+json")
    assert agent_api.authorization.principal_resolutions == 0
    assert agent_api.runtime_provider.calls == 0
    assert agent_api.requirement_connections == []
    with agent_api.database.owner.connect() as db:
        for table in ("agent_run", "agent_attempt", "idempotency_key", "workflow_command"):
            assert db.execute(text(f"SELECT count(*) FROM agent.{table}")).scalar_one() == 0


@pytest.mark.parametrize("operation", ["definitions", "start", "get", "events", "cancel", "resume"])
def test_each_route_rechecks_real_authorization_facade_after_revoke_and_regrant(
    agent_api: AgentApiHarness, operation: str
) -> None:
    started = _start(agent_api).json()
    run_id, attempt_id = started["run"]["id"], started["attempt"]["id"]
    revision = started["attempt"]["revision"]
    if operation == "resume":
        revision = _advance_to_waiting(agent_api, attempt_id)
    capability = {
        "definitions": "agent.definition.read",
        "start": "agent.run.execute",
        "get": "agent.run.read",
        "events": "agent.run.read",
        "cancel": "agent.run.control",
        "resume": "agent.run.control",
    }[operation]
    scope = Scope.platform() if operation == "definitions" else Scope.workspace(WORKSPACE_ID)
    dependencies = authorization_dependencies()

    class CurrentIdentity:
        employee = "employee-facade-current"

        def validate(self, token: str) -> SessionPrincipal | None:
            if token != "session-current":
                return None
            return SessionPrincipal(
                account_id="account-facade-current",
                employee_no=self.employee,
                display_name="Facade User",
                session_kind=SessionKind.FULL,
                is_super_admin=False,
            )

    class CurrentMembership:
        def is_formal_member(self, workspace_id: str, account_id: str) -> bool:
            return workspace_id == WORKSPACE_ID and account_id == "account-facade-current"

    identity = CurrentIdentity()
    decision_dependencies = DecisionDependencies(identity=identity, workspace=CurrentMembership())
    actor = identity.validate("session-current")
    assert actor is not None

    def add_grant() -> Any:
        with agent_api.database.owner.begin() as db:
            return grant(
                db,
                principal_id=actor.account_id,
                capability=capability,
                scope=scope,
                actor=actor,
                reason="Task 6 current authorization test",
                dependencies=dependencies,
            )

    class FacadeAuthorization:
        resolutions = 0
        checks = 0

        def resolve(self, request: Request) -> AuthorizationPrincipal:
            self.resolutions += 1
            with agent_api.database.owner.begin() as db:
                decision = resolve_principal(
                    db,
                    raw_token=request.cookies.get("ep_session", ""),
                    dependencies=dependencies,
                    decision_dependencies=decision_dependencies,
                )
            assert decision.principal is not None
            return decision.principal

        def guard(
            self, principal: AuthorizationPrincipal, requested: str, workspace: str | None
        ) -> None:
            self.checks += 1
            assert requested == capability
            assert workspace == (None if operation == "definitions" else WORKSPACE_ID)
            with agent_api.database.owner.begin() as db:
                allowed = principal_has_capability(
                    db,
                    principal=principal,
                    capability=requested,
                    scope=Scope.platform() if workspace is None else Scope.workspace(workspace),
                    dependencies=dependencies,
                    decision_dependencies=decision_dependencies,
                )
            if not allowed:
                raise HTTPException(status_code=403, detail="Forbidden")

    authorization = FacadeAuthorization()
    client = _client_for(agent_api.runtime, cast(MutableAuthorization, authorization))
    paths = {
        "definitions": "/api/v1/agent-definitions",
        "start": "/api/v1/agent-runs",
        "get": f"/api/v1/agent-runs/{run_id}",
        "events": f"/api/v1/agent-runs/{run_id}/events",
        "cancel": f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/cancel",
        "resume": f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/resume",
    }

    def request() -> Any:
        return client.request(
            "POST" if operation in {"start", "cancel", "resume"} else "GET",
            paths[operation],
            headers=_write_headers(f"facade-{operation}", etag=f'"v{revision}"'),
            json=START_BODY if operation == "start" else None,
        )

    current_grant = add_grant()
    # Resolve now, revoke before the next request, then prove the stale decision is not reused.
    assert request().status_code == (202 if operation in {"start", "cancel", "resume"} else 200)
    with agent_api.database.owner.begin() as db:
        revoke(
            db,
            grant_id=current_grant.id,
            expected_version=current_grant.version,
            actor=actor,
            reason="revoke current permission",
            dependencies=dependencies,
        )
    assert request().status_code == 403
    add_grant()
    assert request().status_code == (202 if operation in {"start", "cancel", "resume"} else 200)
    assert authorization.resolutions == authorization.checks == 3


def test_control_audit_uses_changed_current_employee_not_start_actor(
    agent_api: AgentApiHarness,
) -> None:
    started = _start(agent_api).json()
    agent_api.authorization.principal = agent_api.authorization.principal.model_copy(
        update={"employee_id": "new-current-employee"}
    )
    response = agent_api.client.post(
        f"/api/v1/agent-runs/{started['run']['id']}/attempts/{started['attempt']['id']}/cancel",
        headers=_write_headers("fresh-control-actor", etag=f'"v{started["attempt"]["revision"]}"'),
    )
    assert response.status_code == 202
    with agent_api.database.owner.connect() as db:
        rows = db.execute(
            text(
                "SELECT actor, actor_type FROM audit.audit_event "
                "WHERE action='agent.attempt.cancel'"
            )
        ).all()
    assert [tuple(row) for row in rows] == [("new-current-employee", "EMPLOYEE")]


def test_actor_resolver_accepts_only_exact_current_employee() -> None:
    resolver = CurrentPrincipalActorResolver("employee-not-on-dev-allowlist")
    assert resolver.resolve("employee-not-on-dev-allowlist").actor_type == "EMPLOYEE"
    for forged in ("service-901", "system-901", "employee-901", "other-employee"):
        with pytest.raises(ValueError):
            resolver.resolve(forged)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RequirementNotFound("PRIVATE_REQUIREMENT_SENTINEL"), 404),
        (InvalidRequirementExecutionContext("PRIVATE_REQUIREMENT_SENTINEL"), 422),
        (RequirementDependencyUnavailable("PRIVATE_REQUIREMENT_SENTINEL"), 503),
        (SQLAlchemyError("PRIVATE_REQUIREMENT_SENTINEL"), 503),
    ],
)
def test_requirement_failures_are_typed_sanitized_and_rollback_agent_facts(
    agent_api: AgentApiHarness, error: Exception, status: int
) -> None:
    connections: list[Connection] = []

    class FailingContext(UnboundRequirementExecutionContext):
        def resolve(self, _request: RequirementExecutionRequest) -> RequirementExecutionContext:
            raise error

    def factory(db: Connection) -> FailingContext:
        connections.append(db)
        return FailingContext()

    client = _client_for(
        replace(agent_api.runtime, requirement_context_factory=factory), agent_api.authorization
    )
    response = client.post(
        "/api/v1/agent-runs", headers=_write_headers("requirement-failure"), json=START_BODY
    )
    assert response.status_code == status
    assert "PRIVATE_REQUIREMENT_SENTINEL" not in response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert len(connections) == 1 and connections[0].closed
    with agent_api.database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.idempotency_key")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM agent.agent_run")).scalar_one() == 0


def test_mismatched_requirement_context_returns_typed_invalid_input(
    agent_api: AgentApiHarness,
) -> None:
    class WrongWorkspaceContext(ConnectionBackedRequirementContext):
        def resolve(self, request: RequirementExecutionRequest) -> RequirementExecutionContext:
            return super().resolve(request).model_copy(update={"workspace_id": str(uuid4())})

    client = _client_for(
        replace(agent_api.runtime, requirement_context_factory=WrongWorkspaceContext),
        agent_api.authorization,
    )
    response = client.post(
        "/api/v1/agent-runs", headers=_write_headers("mismatched-context"), json=START_BODY
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {"workspaceId": "INVALID_UUID_SENTINEL"},
        {"definitionVersion": "1"},
        {"definitionVersion": 0},
        {"goal": ""},
        {"goal": "x" * 10_001},
        {"unknown": "EXTRA_SECRET_SENTINEL"},
        {"assignmentId": "00000000-0000-0000-0000-000000009901"},
        {"businessContext": {"assignmentId": "00000000-0000-0000-0000-000000009901"}},
    ],
)
def test_start_rejects_invalid_bounded_inputs_without_sensitive_echo(
    agent_api: AgentApiHarness, changes: dict[str, object]
) -> None:
    response = agent_api.client.post(
        "/api/v1/agent-runs",
        headers=_write_headers("invalid-start-input"),
        json={**START_BODY, **changes},
    )
    assert response.status_code == 422
    assert "INVALID_UUID_SENTINEL" not in response.text
    assert "EXTRA_SECRET_SENTINEL" not in response.text
    assert agent_api.runtime_provider.calls == 0


def test_stale_revision_cross_run_attempt_and_cursor_are_rejected(
    agent_api: AgentApiHarness,
) -> None:
    first, second = _start(agent_api).json(), _start(agent_api, key="another-run-key").json()
    path = f"/api/v1/agent-runs/{first['run']['id']}/attempts/{first['attempt']['id']}/cancel"
    stale = agent_api.client.post(path, headers=_write_headers("stale-revision", etag='"v1"'))
    assert stale.status_code == 409
    assert stale.json()["code"] == "ATTEMPT_REVISION_CONFLICT"
    crossed = agent_api.client.post(
        f"/api/v1/agent-runs/{first['run']['id']}/attempts/{second['attempt']['id']}/cancel",
        headers=_write_headers("crossed-attempt", etag=f'"v{second["attempt"]["revision"]}"'),
    )
    assert crossed.status_code == 404
    _advance_to_waiting(agent_api, first["attempt"]["id"])
    page = agent_api.client.get(f"/api/v1/agent-runs/{first['run']['id']}/events?limit=1").json()
    invalid = agent_api.client.get(
        f"/api/v1/agent-runs/{second['run']['id']}/events", params={"cursor": page["nextCursor"]}
    )
    assert invalid.status_code == 422
    assert (
        agent_api.client.get(
            f"/api/v1/agent-runs/{first['run']['id']}/events?limit=101"
        ).status_code
        == 422
    )
