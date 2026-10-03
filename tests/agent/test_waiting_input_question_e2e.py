from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.agent import accept_workflow_event, get_run
from control_plane.app.modules.agent.adapters.sqlalchemy import (
    SqlAlchemyAgentRepository,
    SqlAlchemyAgentUnitOfWork,
)
from control_plane.app.modules.agent.api import AgentHttpRuntime, routes
from control_plane.app.modules.agent.application.events import _mutation
from control_plane.app.modules.agent.domain import (
    AgentQueryUnavailable,
    AttemptState,
    CheckpointInput,
    EventAcceptanceReceipt,
    transition_attempt,
)
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_business_context_status_e2e import _status_client
from tests.agent.test_events import advance_to_running, dependencies, event, start, waiting_event
from tests.agent.test_final_integrity import facts
from tests.agent.test_repository import DEFINITION, RUN
from tests.agent.test_resume_business_context_e2e import prepare_real_resume
from tests.agent.test_run_queries import seed_run
from tests.agent.test_waiting_input_question import PROMPT, question_event, read_waiting
from tests.source_control.test_v06_production_e2e import Journey, _grant, _revoke, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database


@pytest.mark.parametrize("denied", [None, 401, 403])
def test_detail_authorizes_actual_workspace_before_question_body(
    monkeypatch: pytest.MonkeyPatch, denied: int | None
) -> None:
    order = []
    view, _ = read_waiting(question_event())

    def metadata(*_args: Any, **_kwargs: Any) -> Any:
        order.append("metadata")
        return RUN

    def guard(_principal: Any, capability: str, workspace: str | None) -> None:
        order.append("authorization")
        assert capability == "agent.run.read" and workspace == RUN.workspace_id
        if denied:
            raise HTTPException(denied)

    def details(*_args: Any, **_kwargs: Any) -> Any:
        order.append("body")
        return view

    monkeypatch.setattr(routes, "get_run_metadata", metadata)
    monkeypatch.setattr(routes, "get_run", details)
    app = FastAPI()
    app.include_router(
        routes.create_agent_router(
            lambda: cast(AgentHttpRuntime, SimpleNamespace(dependencies=object())),
            lambda: object(),
            guard,
        )
    )
    with TestClient(app) as client:
        response = client.get(f"/api/v1/agent-runs/{RUN.id}", params={"workspaceId": str(uuid4())})
    assert response.status_code == (denied or 200)
    assert order == (
        ["metadata", "authorization"] if denied else ["metadata", "authorization", "body"]
    )
    if not denied:
        assert response.json()["waitingInput"]["question"] == {"prompt": PROMPT}
        assert response.headers["etag"] == f'"v{view.attempts[0].revision}"'


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["receipt", "audit"])
def test_question_checkpoint_transition_and_receipt_roll_back_together(
    isolated_agent_database: IsolatedAgentDatabase, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    database = isolated_agent_database
    deps = dependencies(database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)
    before = facts(database)

    def transaction(operation: Any) -> Any:
        with database.runtime.begin() as db:
            uow = SqlAlchemyAgentUnitOfWork(db)
            target = uow.repository() if failure == "receipt" else uow
            name = "append_event_receipt" if failure == "receipt" else "append_audit_event"
            monkeypatch.setattr(
                target, name, Mock(side_effect=RuntimeError("synthetic persistence failure"))
            )
            return operation(uow)

    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        accept_workflow_event(
            None,
            event=waiting_event(started.attempt.id),
            dependencies=replace(deps, transaction_runner=transaction),
        )
    assert facts(database) == before
    accepted = accept_workflow_event(
        None, event=waiting_event(started.attempt.id), dependencies=deps
    )
    after = facts(database)
    waiting = get_run(None, run_id=started.run.id, dependencies=deps).waiting_input
    assert waiting is not None and waiting.question.prompt == "请确认受控测试的输入。"
    assert accept_workflow_event(None, event=accepted.event, dependencies=deps) == accepted
    assert facts(database) == after
    assert "请确认受控测试的输入。" not in str(after["audit"])


@pytest.mark.integration
def test_persisted_predecessor_missing_question_reads_null_and_replays_unchanged(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    database = isolated_agent_database
    deps = dependencies(database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)
    incoming = waiting_event(started.attempt.id)
    data = dict(incoming.data)
    data.pop("question")
    predecessor = incoming.model_copy(update={"data": data})
    checkpoint = CheckpointInput.model_validate(data["checkpoint"])
    # Seed the predecessor's accepted facts using the owned append/CAS interfaces.
    with database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        current = repository.attempt_by_id(started.attempt.id, for_update=True)
        assert current is not None
        checkpoint = repository.insert_checkpoint(current.id, checkpoint)
        waiting = transition_attempt(
            current, AttemptState.WAITING_INPUT, checkpoint=checkpoint, now=deps.clock()
        )
        persisted = repository.compare_and_set_attempt(
            current.id,
            expected_revision=current.revision,
            mutation=_mutation(
                waiting,
                checkpoint=checkpoint,
                waiting_deadline=datetime.fromisoformat(str(data["waitingDeadline"])),
                now=deps.clock(),
            ).model_copy(update={"event_sequence": predecessor.sequence}),
        )
        assert persisted is not None
        repository.append_event(predecessor)
        repository.append_event_receipt(
            EventAcceptanceReceipt(
                event_id=predecessor.id, attempt=persisted, checkpoint=checkpoint
            )
        )
    before = facts(database)
    assert get_run(None, run_id=started.run.id, dependencies=deps).waiting_input is None
    replay = accept_workflow_event(None, event=predecessor, dependencies=deps)
    assert replay.event.model_dump(mode="json") == predecessor.model_dump(mode="json")
    assert replay.attempt == persisted
    with pytest.raises(ValueError, match="question"):
        accept_workflow_event(
            None,
            event=predecessor.model_copy(
                update={"id": str(uuid4()), "sequence": predecessor.sequence + 1}
            ),
            dependencies=deps,
        )
    assert facts(database) == before


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["cancel", "resume"])
def test_real_session_question_access_is_scoped_and_does_not_block_controls(
    journey: Journey, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    subject = prepare_real_resume(journey)
    with journey.database.engines["agent"].begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        foreign, _, _ = seed_run(repository, 900, str(UUID(int=999)))
    state: dict[str, Any] = {"reads": [], "broken": False}

    def transaction(callback: Any) -> Any:
        with journey.database.engines["agent"].begin() as db:
            uow = SqlAlchemyAgentUnitOfWork(db)
            repository = uow.repository()
            original = repository.event_by_position

            def read(attempt_id: str, *, generation: int, sequence: int) -> Any:
                state["reads"].append((attempt_id, generation, sequence))
                if state["broken"]:
                    raise AgentQueryUnavailable("synthetic question storage failure")
                return original(attempt_id, generation=generation, sequence=sequence)

            monkeypatch.setattr(repository, "event_by_position", read)
            return callback(uow)

    runtime = replace(
        subject.runtime,
        dependencies=replace(subject.runtime.dependencies, transaction_runner=transaction),
    )
    path = f"/api/v1/agent-runs/{subject.run.id}"
    with _status_client(journey, runtime) as client:
        client.cookies.clear()
        assert client.get(path).status_code == 401
        client.cookies.update(journey.leader.cookies)
        assert client.get(path).status_code == 403
        client.cookies.clear()
        client.cookies.update(journey.member.cookies)
        assert client.get(f"/api/v1/agent-runs/{foreign.id}").status_code == 403
        assert state["reads"] == []
        response = client.get(path)
        assert response.status_code == 200, response.text
        projection = response.json()["waitingInput"]
        assert projection["attemptId"] == subject.attempt.id
        assert projection["generation"] == subject.attempt.runner_generation
        assert projection["checkpointId"] == subject.attempt.checkpoint.id
        assert projection["question"] == {"prompt": "请确认受控测试的输入。"}
        assert projection["waitingDeadline"] == response.json()["attempts"][0]["waitingDeadline"]
        assert response.headers["etag"] == subject.etag
        state["broken"] = True
        assert client.get(path).status_code == 503
        reads = len(state["reads"])
        assert client.get(path + "/business-context-status").status_code == 200
        _revoke(journey, journey.member_id, "agent.run.read")
        assert client.get(path).status_code == 403
        controlled = _write(
            client,
            subject.path.replace("/resume", f"/{operation}"),
            {},
            etag=subject.etag,
            key=f"question-{operation}",
            status=202,
        )
        assert len(state["reads"]) == reads
        _grant(journey.admin, journey.member_id, "agent.run.read", journey.workspace_id)
        assert client.get(path).json()["waitingInput"] is None
        assert len(state["reads"]) == reads
        if operation == "resume":
            state["broken"] = False
            generation = controlled.json()["attempt"]["runnerGeneration"]
            for sequence, kind in ((1, "ATTEMPT_PROVISIONING"), (2, "ATTEMPT_RUNNING")):
                accept_workflow_event(
                    None,
                    event=event(
                        subject.attempt.id,
                        event_id=str(uuid4()),
                        event_type=kind,
                        sequence=sequence,
                        generation=generation,
                    ),
                    dependencies=runtime.dependencies,
                )
            next_event = waiting_event(subject.attempt.id, generation=generation, sequence=3)
            next_data = next_event.model_dump(mode="python")["data"]
            next_data["checkpoint"]["id"] = str(uuid4())
            next_data["question"] = {"prompt": "第二次等待\n只展示当前问题"}
            next_data["waitingDeadline"] = subject.deadline.isoformat()
            next_event = next_event.model_copy(update={"id": str(uuid4()), "data": next_data})
            accept_workflow_event(None, event=next_event, dependencies=runtime.dependencies)
            current = client.get(path)
            assert current.status_code == 200, current.text
            assert current.json()["waitingInput"]["eventId"] == next_event.id
            assert current.json()["waitingInput"]["generation"] == generation
            assert current.json()["waitingInput"]["question"] == next_data["question"]
