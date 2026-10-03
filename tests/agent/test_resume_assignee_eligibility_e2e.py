from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from typing import Any, cast
from unittest.mock import Mock

import pytest
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.requirement import get_requirement_for_update
from control_plane.app.modules.requirement.adapters.assignment import (
    ComposedAutomaticAssignmentGuard,
)
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_business_context_status_e2e import _status_client
from tests.agent.test_control import assert_postgres_blocked_by
from tests.agent.test_resume_business_context_e2e import agent_evidence, prepare_real_resume
from tests.source_control.test_v06_production_e2e import Journey, _grant, _revoke, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database


def prepare_controller(journey: Journey) -> Any:
    subject = prepare_real_resume(journey)
    assert isinstance(
        bootstrap.requirement_dependencies().assignment_guard, ComposedAutomaticAssignmentGuard
    )
    for capability in ("agent.run.read", "agent.run.control"):
        _grant(journey.admin, journey.leader_id, capability, journey.workspace_id)
    return subject


def select_controller(client: Any, journey: Journey) -> None:
    client.cookies.clear()
    client.cookies.update(journey.leader.cookies)


@pytest.mark.integration
@pytest.mark.parametrize("change", ["account", "grant"])
def test_selected_assignee_can_lose_eligibility_without_changing_assignment_or_controller(
    journey: Journey,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    subject = prepare_controller(journey)
    assert journey.member_id != journey.leader_id
    calls: list[dict[str, Any]] = []
    original = ComposedAutomaticAssignmentGuard.can_assign

    def explicit(guard: ComposedAutomaticAssignmentGuard, **values: Any) -> bool:
        calls.append(values)
        return original(guard, **values)

    monkeypatch.setattr(ComposedAutomaticAssignmentGuard, "can_assign", explicit)
    automatic = Mock(side_effect=AssertionError("no automatic assignment in resume or reads"))
    monkeypatch.setattr(ComposedAutomaticAssignmentGuard, "can_auto_assign", automatic)
    owner_path = f"/api/v1/requirements/{subject.body['requirementId']}"
    run_path = f"/api/v1/agent-runs/{subject.run.id}"
    with _status_client(journey, subject.runtime) as client:
        select_controller(client, journey)
        owner = client.get(owner_path).json()
        work = next(row for row in owner["workItems"] if row["id"] == subject.body["workItemId"])
        assignment = next(
            row for row in owner["workItemAssignments"] if row["workItemId"] == work["id"]
        )
        assert work["humanOwnerId"] == assignment["assigneeId"] == journey.member_id
        assert assignment["id"] == subject.source["assignmentId"]
        capability = work["requiredCapabilities"][0]
        if change == "account":
            accounts = journey.admin.get("/api/v1/admin/accounts").json()
            assert accounts["nextCursor"] is None
            account = next(row for row in accounts["items"] if row["id"] == journey.member_id)
            disabled = _write(
                journey.admin,
                f"/api/v1/admin/accounts/{journey.member_id}/disable",
                {"reason": "Synthetic assignee eligibility withdrawal"},
                etag=account["etag"],
                status=204,
            )
        else:
            _revoke(journey, journey.member_id, capability)
        before = agent_evidence(journey)
        assert client.get(run_path + "/business-context-status").json()["currentness"] == "CURRENT"
        assert client.get(run_path).json()["waitingInput"] is not None
        assert calls == []
        refused = _write(
            client, subject.path, {}, etag=subject.etag, key="assignee-denied", status=409
        )
        assert "code" not in refused.json()
        assert agent_evidence(journey) == before
        assert calls == [
            {
                "actor_id": journey.member_id,
                "workspace_id": journey.workspace_id,
                "repository_id": work["repositoryId"],
                "required_capabilities": tuple(work["requiredCapabilities"]),
            }
        ]
        unchanged = client.get(owner_path).json()
        assert unchanged["workItemAssignments"] == owner["workItemAssignments"]
        if change == "account":
            _write(
                journey.admin,
                f"/api/v1/admin/accounts/{journey.member_id}/enable",
                {"reason": "Synthetic assignee eligibility restoration"},
                etag=disabled.headers["etag"],
                status=204,
            )
        else:
            _grant(journey.admin, journey.member_id, capability, journey.workspace_id)
        accepted = _write(
            client, subject.path, {}, etag=subject.etag, key="assignee-accepted", status=202
        )
        assert len(calls) == 2
        assert accepted.json()["attempt"]["bindingId"] == subject.attempt.binding_id
        assert (
            accepted.json()["attempt"]["runnerGeneration"] == subject.attempt.runner_generation + 1
        )
        _revoke(journey, journey.member_id, capability)
        before_replay = agent_evidence(journey)
        forbidden = Mock(side_effect=AssertionError("replay must not obtain owner or eligibility"))
        with _status_client(
            journey, replace(subject.runtime, requirement_context_factory=forbidden)
        ) as replay_client:
            select_controller(replay_client, journey)
            replay = _write(
                replay_client,
                subject.path,
                {},
                etag=subject.etag,
                key="assignee-accepted",
                status=202,
            )
            assert (
                replay.content == accepted.content
                and replay.headers["etag"] == accepted.headers["etag"]
            )
            assert agent_evidence(journey) == before_replay
            _revoke(journey, journey.leader_id, "agent.run.control")
            _write(
                replay_client,
                subject.path,
                {},
                etag=subject.etag,
                key="assignee-accepted",
                status=403,
            )
        forbidden.assert_not_called()
        assert len(calls) == 2
    automatic.assert_not_called()


@pytest.mark.integration
def test_eligibility_dependency_failure_is_503_and_cancel_reads_do_not_check_it(
    journey: Journey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = prepare_controller(journey)
    guard = Mock(side_effect=RuntimeError("private-eligibility-dependency-sentinel"))
    monkeypatch.setattr(ComposedAutomaticAssignmentGuard, "can_assign", guard)
    connections: list[Any] = []

    def factory(db: Any) -> Any:
        connections.append(db)
        return subject.runtime.requirement_context_factory(db)

    with _status_client(
        journey, replace(subject.runtime, requirement_context_factory=factory)
    ) as client:
        select_controller(client, journey)
        before = agent_evidence(journey)
        response = _write(client, subject.path, {}, etag=subject.etag, status=503)
        assert "private-eligibility" not in response.text
        assert agent_evidence(journey) == before
        guard.assert_called_once()
        assert len(connections) == 1 and connections[0].closed
        path = f"/api/v1/agent-runs/{subject.run.id}"
        assert client.get(path).json()["waitingInput"] is not None
        assert client.get(path + "/business-context-status").json()["currentness"] == "CURRENT"
        _write(
            client, subject.path.replace("/resume", "/cancel"), {}, etag=subject.etag, status=202
        )
        guard.assert_called_once()
    assert all(db.closed for db in connections)


@pytest.mark.integration
def test_eligibility_wait_holds_owner_protection_and_cannot_extend_deadline(
    journey: Journey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = prepare_controller(journey)
    entered, release, contender_ready = Event(), Event(), Event()
    owner_pids: list[int] = []
    contender_pids: list[int] = []
    connections: list[Any] = []
    original = ComposedAutomaticAssignmentGuard.can_assign

    def delayed(guard: ComposedAutomaticAssignmentGuard, **values: Any) -> bool:
        assert original(guard, **values) is True
        entered.set()
        assert release.wait(10), "test did not release eligibility check"
        return True

    monkeypatch.setattr(ComposedAutomaticAssignmentGuard, "can_assign", delayed)

    def factory(db: Any) -> Any:
        connections.append(db)
        owner_pids.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        db.rollback()
        return subject.runtime.requirement_context_factory(db)

    def protected_reader() -> Any:
        with journey.database.engines["requirement"].begin() as db:
            contender_pids.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            contender_ready.set()
            return get_requirement_for_update(
                db,
                requirement_id=subject.body["requirementId"],
                dependencies=bootstrap.requirement_dependencies(),
            )

    before = agent_evidence(journey)
    with (
        _status_client(
            journey, replace(subject.runtime, requirement_context_factory=factory)
        ) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        select_controller(client, journey)
        resume = pool.submit(_write, client, subject.path, {}, etag=subject.etag, status=409)
        try:
            assert entered.wait(5)
            reader = pool.submit(protected_reader)
            assert contender_ready.wait(5)
            assert_postgres_blocked_by(
                cast(IsolatedAgentDatabase, journey.database),
                waiting_pid=contender_pids[0],
                blocking_pid=owner_pids[0],
            )
            subject.clock.advance_to(subject.deadline)
        finally:
            release.set()
        assert resume.result(timeout=10).status_code == 409
        reader.result(timeout=10)
    assert agent_evidence(journey) == before
    assert len(connections) == 1 and connections[0].closed
