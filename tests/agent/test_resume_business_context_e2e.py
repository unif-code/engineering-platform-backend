from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.agent import accept_workflow_event, get_run
from control_plane.app.modules.agent.adapters.sqlalchemy import (
    SqlAlchemyAgentRepository,
    SqlAlchemyAgentUnitOfWork,
)
from control_plane.app.modules.agent.api import AgentHttpRuntime, routes
from control_plane.app.modules.agent.application.errors import InvalidRequirementExecutionContext
from control_plane.app.modules.requirement import (
    RequirementNotFound,
    WorkItemAssigneeIneligible,
    acknowledge_repository_binding_request,
    assign_work_item,
    claim_repository_binding_requests,
    get_requirement,
    get_requirement_for_update,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_business_context_status_e2e import (
    _evidence,
    _start_source_run,
    _status_client,
)
from tests.agent.test_control import ControllableClock, assert_postgres_blocked_by
from tests.agent.test_events import advance_to_running, waiting_event
from tests.agent.test_repository import ATTEMPT, DEFINITION, RUN
from tests.agent.test_resume_business_context import resume_application as resume_application
from tests.agent.test_run_queries import seed_run
from tests.agent.test_start_run import start_application as start_application
from tests.source_control.test_v06_production_e2e import Journey, _grant, _revoke, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database


@pytest.fixture
def resume_api(
    resume_application: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    h = resume_application
    monkeypatch.setattr(routes, "get_run_metadata", Mock(return_value=RUN))
    owner = MagicMock()
    factory = Mock(return_value=h.port)
    runtime = AgentHttpRuntime(
        engine=Mock(),
        dependencies=h.dependencies,
        requirement_engine=owner,
        requirement_context_factory=factory,
    )
    guard = Mock()
    principal = SimpleNamespace(employee_id="employee-901")
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(routes.create_agent_router(lambda: runtime, lambda: principal, guard))
    with TestClient(app, base_url="https://testserver") as client:
        h.client, h.owner, h.factory, h.guard = client, owner, factory, guard
        yield h


def post(h: Any, *, key: str = "resume-http", revision: int = 1, operation: str = "resume") -> Any:
    return h.client.post(
        f"/api/v1/agent-runs/{RUN.id}/attempts/{ATTEMPT.id}/{operation}",
        headers={
            "Origin": "https://testserver",
            "Idempotency-Key": key,
            "If-Match": f'"v{revision}"',
        },
        json={},
    )


def test_http_resume_uses_lazy_request_connection_and_replay_or_conflict_never_reopens_it(
    resume_api: Any,
) -> None:
    h = resume_api
    accepted = post(h)
    assert accepted.status_code == 202, accepted.text
    h.owner.connect.assert_called_once()
    h.factory.assert_called_once_with(h.owner.connect.return_value.__enter__.return_value)
    h.owner.connect.return_value.__exit__.assert_called_once()
    h.owner.connect.side_effect = AssertionError("historical replay must not open owner")
    replay = post(h)
    assert replay.status_code == 202 and replay.content == accepted.content
    assert replay.headers["etag"] == accepted.headers["etag"]
    conflict = post(h, revision=2)
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    h.owner.connect.assert_called_once()


@pytest.mark.parametrize("case", ["unauthorized", "forbidden", "null", "revision"])
def test_http_preflight_and_local_refusal_do_not_connect_owner(resume_api: Any, case: str) -> None:
    h = resume_api
    h.owner.connect.side_effect = AssertionError("refusal must not open owner")
    expected = 409
    if case in ("unauthorized", "forbidden"):
        expected = 401 if case == "unauthorized" else 403
        h.guard.side_effect = HTTPException(expected)
    if case == "null":
        h.repository.run_by_id.return_value = RUN.model_copy(update={"business_context": None})
    result = post(h, revision=99 if case == "revision" else 1)
    assert result.status_code == expected, result.text
    h.owner.connect.assert_not_called()
    h.port.protect.assert_not_called()


@pytest.mark.parametrize("stage", ["connect", "factory", "protect", "unbound"])
def test_http_owner_protection_failure_is_unavailable_without_raw_diagnostics(
    resume_api: Any, stage: str
) -> None:
    h = resume_api
    if stage == "unbound":
        h.factory.return_value = SimpleNamespace(resolve=Mock())
    else:
        target = {"connect": h.owner.connect, "factory": h.factory, "protect": h.port.protect}[
            stage
        ]
        target.side_effect = RuntimeError("private-owner-connection-sentinel")
    result = post(h)
    assert result.status_code == 503, result.text
    assert "private-owner" not in result.text
    h.repository.compare_and_set_attempt.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_http_owner_authorization_error_remains_authorization_failure(
    resume_api: Any, status: int
) -> None:
    h = resume_api
    h.port.protect.side_effect = HTTPException(status)
    assert post(h).status_code == status
    h.repository.compare_and_set_attempt.assert_not_called()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RequirementNotFound("missing"), 409),
        (InvalidRequirementExecutionContext("unknown"), 503),
        (WorkItemAssigneeIneligible("private-assignee"), 409),
    ],
)
def test_http_owner_refusal_does_not_reuse_start_mapping(
    resume_api: Any, error: Exception, status: int
) -> None:
    resume_api.port.protect.side_effect = error
    assert post(resume_api).status_code == status


def prepare_real_resume(journey: Journey) -> SimpleNamespace:
    body, started = _start_source_run(journey)
    _grant(journey.admin, journey.member_id, "agent.run.control", journey.workspace_id)
    runtime = bootstrap.agent_http_runtime()
    clock = ControllableClock(runtime.dependencies.clock())
    runtime = replace(runtime, dependencies=replace(runtime.dependencies, clock=clock))
    attempt_id = started.json()["attempt"]["id"]
    advance_to_running(runtime.dependencies, attempt_id)
    waiting = waiting_event(attempt_id)
    deadline = clock() + timedelta(minutes=5)
    accept_workflow_event(
        None,
        event=waiting.model_copy(
            update={"data": {**waiting.data, "waitingDeadline": deadline.isoformat()}}
        ),
        dependencies=runtime.dependencies,
    )
    owner_dependencies = bootstrap.requirement_dependencies()
    now = owner_dependencies.clock.now()
    with journey.database.engines["requirement"].begin() as db:
        messages = claim_repository_binding_requests(
            db,
            limit=1,
            available_before=now,
            lease_until=now + timedelta(minutes=1),
            dependencies=owner_dependencies,
        )
        assert len(messages) == 1 and messages[0].requirement_id == body["requirementId"]
        result = acknowledge_repository_binding_request(
            db,
            message_id=messages[0].message_id,
            consumer="SOURCE_CONTROL",
            dependencies=owner_dependencies,
        )
        assert result.state.value == "PREPARING"
    view = get_run(None, run_id=started.json()["run"]["id"], dependencies=runtime.dependencies)
    return SimpleNamespace(
        runtime=runtime,
        body=body,
        run=view.run,
        attempt=view.attempts[0],
        path=f"/api/v1/agent-runs/{view.run.id}/attempts/{attempt_id}/resume",
        source=started.json()["run"]["businessContext"],
        etag=f'"v{view.attempts[0].revision}"',
        clock=clock,
        deadline=deadline,
    )


def reassign(journey: Journey, subject: Any, db: Any) -> Any:
    dependencies = bootstrap.requirement_dependencies()
    owner = get_requirement(
        db, requirement_id=subject.body["requirementId"], dependencies=dependencies
    )
    work = next(item for item in owner.work_items if item.id == subject.body["workItemId"])
    return assign_work_item(
        db,
        requirement_id=subject.body["requirementId"],
        work_item_id=work.id,
        human_owner_id=journey.leader_id,
        reason="Synthetic protected resume race",
        expected_revision=work.revision,
        actor=SimpleNamespace(account_id=journey.member_id),
        idempotency_key="resume-guard-reassign",
        dependencies=dependencies,
    )


def agent_evidence(journey: Journey) -> Any:
    facts, receipts, audits = _evidence(journey)
    with journey.database.owner.connect() as db:
        checkpoints = (
            db.execute(text("SELECT to_jsonb(t) FROM agent.checkpoint t ORDER BY 1"))
            .scalars()
            .all()
        )
    return (
        facts,
        receipts,
        [row for row in audits if row["action"].startswith("agent.")],
        checkpoints,
    )


@pytest.mark.integration
@pytest.mark.parametrize("close_fails", [False, True])
def test_default_session_resume_replay_survives_reassignment_outage_and_expiry(
    journey: Journey,
    close_fails: bool,
) -> None:
    subject = prepare_real_resume(journey)

    def factory(db: Any) -> Any:
        delegate = subject.runtime.requirement_context_factory(db)

        @contextmanager
        def protect(request: Any, *, expected_assignment_id: str) -> Iterator[Any]:
            with delegate.protect(
                request, expected_assignment_id=expected_assignment_id
            ) as current:
                yield current
            if close_fails:
                raise SQLAlchemyError("synthetic owner cleanup failure after Agent commit")

        return SimpleNamespace(resolve=delegate.resolve, protect=protect)

    with _status_client(
        journey, replace(subject.runtime, requirement_context_factory=factory)
    ) as client:
        accepted = _write(
            client,
            subject.path,
            {},
            etag=subject.etag,
            key="accepted-resume",
            status=503 if close_fails else 202,
        )
    with journey.database.engines["requirement"].begin() as db:
        changed = reassign(journey, subject, db)
    assert changed.assignment.id != subject.run.business_context.assignment_id
    subject.clock.advance_to(subject.deadline + timedelta(days=1))
    owner = Mock(side_effect=AssertionError("accepted replay must not open owner"))
    runtime = replace(subject.runtime, requirement_engine=SimpleNamespace(connect=owner))
    before = agent_evidence(journey)
    with _status_client(journey, runtime) as client:
        replay = _write(
            client, subject.path, {}, etag=subject.etag, key="accepted-resume", status=202
        )
        if not close_fails:
            assert (
                replay.content == accepted.content
                and replay.headers["etag"] == accepted.headers["etag"]
            )
        assert replay.json()["attempt"]["revision"] == subject.attempt.revision + 1
        assert replay.headers["etag"] == f'"v{subject.attempt.revision + 1}"'
        conflict = _write(client, subject.path, {}, etag='"v99"', key="accepted-resume", status=409)
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
        assert agent_evidence(journey) == before
        view = client.get(f"/api/v1/agent-runs/{subject.run.id}").json()
        assert view["run"]["businessContext"] == subject.source
        assert view["attempts"][0]["bindingId"] == subject.attempt.binding_id
        _revoke(journey, journey.member_id, "agent.run.control")
        _write(client, subject.path, {}, etag=subject.etag, key="accepted-resume", status=403)
    owner.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize("case", ["reassigned", "deadline"])
def test_owner_first_waits_then_refuses_new_resume_without_agent_changes(
    journey: Journey, case: str
) -> None:
    subject = prepare_real_resume(journey)
    owner_ready = Event()
    owner_pids: list[int] = []
    connections: list[Any] = []

    def factory(db: Any) -> Any:
        connections.append(db)
        owner_pids.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        db.rollback()
        owner_ready.set()
        return subject.runtime.requirement_context_factory(db)

    runtime = replace(subject.runtime, requirement_context_factory=factory)
    before = agent_evidence(journey)
    with _status_client(journey, runtime) as client, ThreadPoolExecutor(max_workers=1) as pool:
        with journey.database.engines["requirement"].begin() as owner_db:
            owner_pid = owner_db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            get_requirement_for_update(
                owner_db,
                requirement_id=subject.body["requirementId"],
                dependencies=bootstrap.requirement_dependencies(),
            )
            if case == "reassigned":
                reassign(journey, subject, owner_db)
            future = pool.submit(
                _write, client, subject.path, {}, etag=subject.etag, key="owner-first", status=409
            )
            assert owner_ready.wait(5)
            assert_postgres_blocked_by(
                cast(IsolatedAgentDatabase, journey.database),
                waiting_pid=owner_pids[0],
                blocking_pid=owner_pid,
            )
            assert not future.done()
            if case == "deadline":
                subject.clock.advance_to(subject.deadline)
        assert future.result(timeout=10).status_code == 409
    assert agent_evidence(journey) == before
    assert len(connections) == 1 and connections[0].closed


@pytest.mark.integration
@pytest.mark.parametrize("failure", [None, "workflow", "audit", "before-commit"])
def test_resume_owner_lock_survives_agent_commit_or_rollback_boundary(
    journey: Journey, failure: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = prepare_real_resume(journey)
    before = agent_evidence(journey)
    agent_ready, release_agent, assignment_ready = Event(), Event(), Event()
    owner_active = Event()
    owner_pids: list[int] = []
    assignment_pids: list[int] = []
    connections: list[Any] = []

    def factory(db: Any) -> Any:
        connections.append(db)
        owner_pids.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        db.rollback()
        delegate = subject.runtime.requirement_context_factory(db)

        @contextmanager
        def protect(request: Any, *, expected_assignment_id: str) -> Iterator[Any]:
            with delegate.protect(
                request, expected_assignment_id=expected_assignment_id
            ) as current:
                owner_active.set()
                try:
                    yield current
                finally:
                    owner_active.clear()

        return SimpleNamespace(resolve=delegate.resolve, protect=protect)

    def transaction(operation: Any) -> Any:
        with journey.database.engines["agent"].begin() as db:
            uow = SqlAlchemyAgentUnitOfWork(db)
            if failure == "workflow":
                monkeypatch.setattr(
                    uow.repository(),
                    "insert_workflow_command",
                    Mock(side_effect=SQLAlchemyError("synthetic workflow failure")),
                )
            if failure == "audit":
                monkeypatch.setattr(
                    uow,
                    "append_audit_event",
                    Mock(side_effect=SQLAlchemyError("synthetic audit failure")),
                )
            try:
                result = operation(uow)
                if failure == "before-commit" and owner_active.is_set():
                    raise SQLAlchemyError("synthetic transaction failure")
                return result
            finally:
                # The same runner first serves the authorization read without owner protection.
                if owner_active.is_set():
                    agent_ready.set()
                    assert release_agent.wait(10), "test did not release Agent transaction"

    def change_assignment() -> Any:
        with journey.database.engines["requirement"].begin() as db:
            assignment_pids.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            assignment_ready.set()
            return reassign(journey, subject, db)

    runtime = replace(
        subject.runtime,
        dependencies=replace(subject.runtime.dependencies, transaction_runner=transaction),
        requirement_context_factory=factory,
    )
    with _status_client(journey, runtime) as client, ThreadPoolExecutor(max_workers=2) as pool:
        resume = pool.submit(
            _write,
            client,
            subject.path,
            {},
            etag=subject.etag,
            key="resume-first",
            status=503 if failure else 202,
        )
        try:
            assert agent_ready.wait(5)
            reassignment = pool.submit(change_assignment)
            assert assignment_ready.wait(5)
            assert_postgres_blocked_by(
                cast(IsolatedAgentDatabase, journey.database),
                waiting_pid=assignment_pids[0],
                blocking_pid=owner_pids[0],
            )
            assert not reassignment.done()
        finally:
            release_agent.set()
        response = resume.result(timeout=10)
        reassignment.result(timeout=10)
    assert connections and all(db.closed for db in connections)
    if failure:
        assert agent_evidence(journey) == before
    else:
        assert (
            response.json()["attempt"]["runnerGeneration"] == subject.attempt.runner_generation + 1
        )
        assert response.json()["attempt"]["bindingId"] == subject.attempt.binding_id
        facts, _, audits, _ = agent_evidence(journey)
        assert len([row for row in facts[-1] if row["kind"] == "RESUME"]) == 1
        assert len([row for row in audits if row["action"] == "agent.attempt.resume"]) == 1


@pytest.mark.integration
def test_default_session_denial_cross_workspace_and_cancel_never_need_owner(
    journey: Journey,
) -> None:
    subject = prepare_real_resume(journey)
    with journey.database.engines["agent"].begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        foreign, foreign_attempt, _ = seed_run(repository, 900, str(UUID(int=999)))
    owner = Mock(side_effect=RuntimeError("private-owner-outage"))
    runtime = replace(subject.runtime, requirement_engine=SimpleNamespace(connect=owner))
    with _status_client(journey, runtime) as client:
        client.cookies.clear()
        _write(client, subject.path, {}, etag=subject.etag, status=401)
        client.cookies.update(journey.leader.cookies)
        _write(client, subject.path, {}, etag=subject.etag, status=403)
        client.cookies.clear()
        client.cookies.update(journey.member.cookies)
        _write(
            client,
            f"/api/v1/agent-runs/{foreign.id}/attempts/{foreign_attempt.id}/resume",
            {},
            etag='"v1"',
            status=403,
        )
        owner.assert_not_called()
        before = agent_evidence(journey)
        _write(client, subject.path, {}, etag=subject.etag, key="owner-outage", status=503)
        assert agent_evidence(journey) == before
        owner.assert_called_once()
        _write(
            client, subject.path.replace("/resume", "/cancel"), {}, etag=subject.etag, status=202
        )
        owner.assert_called_once()
