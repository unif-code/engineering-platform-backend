from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from control_plane.app.modules.agent.application import events, queries
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.domain import (
    AgentQueryUnavailable,
    AttemptState,
    CanonicalEventInput,
    EventAcceptanceReceipt,
    EventReplayConflict,
)
from control_plane.app.modules.agent.ports import AgentRepository
from tests.agent.test_events import waiting_event
from tests.agent.test_repository import ATTEMPT, BINDING, NOW, RUN
from tests.agent.test_start_run import start_application as start_application

PROMPT = "  请确认目标环境：\n<em>原文</em> https://example.invalid/question  "


def question_event(question: Any = None, *, legacy: bool = False) -> CanonicalEventInput:
    payload = waiting_event(ATTEMPT.id, sequence=1).model_dump(mode="python")
    payload["data"].pop("question", None)
    if not legacy:
        payload["data"]["question"] = {"prompt": PROMPT} if question is None else question
    return CanonicalEventInput.model_validate(payload)


@pytest.fixture
def question_application(start_application: Any) -> SimpleNamespace:
    dependencies, repository, _ = start_application
    uow = Mock()
    uow.repository.return_value = repository
    running = ATTEMPT.model_copy(update={"state": AttemptState.RUNNING})
    repository.run_by_id.return_value = RUN
    repository.attempt_by_id.return_value = running
    repository.event_by_id.return_value = None
    repository.insert_checkpoint.side_effect = lambda _attempt_id, checkpoint: checkpoint
    repository.compare_and_set_attempt.side_effect = lambda _id, expected_revision, mutation: (
        running.model_copy(
            update={**mutation.model_dump(exclude={"now"}), "revision": expected_revision + 1}
        )
    )
    return SimpleNamespace(
        repository=repository,
        uow=uow,
        dependencies=replace(dependencies, transaction_runner=lambda operation: operation(uow)),
    )


def test_question_is_preserved_in_event_and_receipt_without_becoming_audit_text(
    question_application: Any, caplog: pytest.LogCaptureFixture
) -> None:
    h = question_application
    event = question_event()
    accepted = events.accept_workflow_event(event, dependencies=h.dependencies)
    assert accepted.event.data["question"] == {"prompt": PROMPT}
    assert accepted.attempt.state is AttemptState.WAITING_INPUT
    assert accepted.checkpoint == accepted.attempt.checkpoint
    h.repository.append_event.assert_called_once_with(event)
    h.repository.append_event_receipt.assert_called_once()
    assert PROMPT not in h.uow.append_audit_event.call_args.args[0].model_dump_json()
    assert PROMPT not in caplog.text


@pytest.mark.parametrize(
    "question",
    [
        {},
        {"prompt": ""},
        {"prompt": " \t\n"},
        {"prompt": 5},
        {"prompt": "x" * 10001},
        {"prompt": "x", "answer": "no"},
        "text",
        None,
    ],
)
def test_malformed_question_cannot_be_read_or_accepted(question: Any) -> None:
    data = question_event(legacy=True).model_dump(mode="python")
    data["data"]["question"] = question
    with pytest.raises(ValueError):
        CanonicalEventInput.model_validate(data)


def test_question_uses_existing_summary_limit_and_preserves_original_whitespace() -> None:
    assert question_event({"prompt": "x" * 10000}).data["question"] == {"prompt": "x" * 10000}
    assert question_event().data["question"] == {"prompt": PROMPT}


def test_new_event_without_question_is_rejected_before_any_mutation(
    question_application: Any,
) -> None:
    h = question_application
    with pytest.raises(ValueError, match="question"):
        events.accept_workflow_event(question_event(legacy=True), dependencies=h.dependencies)
    h.repository.insert_checkpoint.assert_not_called()
    h.repository.compare_and_set_attempt.assert_not_called()
    h.repository.append_event.assert_not_called()
    h.uow.append_audit_event.assert_not_called()


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_event_replay_keeps_old_payload_and_receipt(
    question_application: Any, legacy: bool
) -> None:
    h = question_application
    event = question_event(legacy=legacy)
    from control_plane.app.modules.agent.domain import CheckpointInput

    checkpoint = CheckpointInput.model_validate(event.data["checkpoint"])
    waiting = ATTEMPT.model_copy(
        update={"state": AttemptState.WAITING_INPUT, "checkpoint": checkpoint}
    )
    h.repository.event_by_id.return_value = event
    h.repository.event_receipt_by_id.return_value = EventAcceptanceReceipt(
        event_id=event.id, attempt=waiting, checkpoint=checkpoint
    )
    digest = events._canonical_digest(event)
    result = events.accept_workflow_event(event, dependencies=h.dependencies)
    assert result.event.model_dump() == event.model_dump()
    assert events._canonical_digest(result.event) == digest
    assert result.attempt == waiting
    changed = event.model_dump(mode="python")
    changed["data"]["question"] = {"prompt": "changed"}
    with pytest.raises(EventReplayConflict):
        events.accept_workflow_event(
            CanonicalEventInput.model_validate(changed), dependencies=h.dependencies
        )
    h.repository.insert_checkpoint.assert_not_called()
    h.repository.append_event.assert_not_called()
    h.uow.append_audit_event.assert_not_called()


def read_waiting(event: CanonicalEventInput | None, *, attempt: Any = None) -> tuple[Any, Mock]:
    from control_plane.app.modules.agent.domain import CheckpointInput

    base = question_event(legacy=True)
    current = (
        ATTEMPT.model_copy(
            update={
                "state": AttemptState.WAITING_INPUT,
                "event_sequence": base.sequence,
                "checkpoint": CheckpointInput.model_validate(base.data["checkpoint"]),
                "waiting_deadline": datetime.fromisoformat(str(base.data["waitingDeadline"])),
            }
        )
        if attempt is None
        else attempt
    )
    repository = Mock(spec=AgentRepository)
    repository.run_by_id.return_value = RUN
    repository.attempts_by_run_id.return_value = (current,)
    repository.binding_by_attempt_id.return_value = BINDING
    repository.event_by_position = Mock(return_value=event)
    dependencies = cast(
        AgentDependencies,
        SimpleNamespace(
            transaction_runner=lambda operation: operation(
                SimpleNamespace(repository=lambda: repository)
            )
        ),
    )
    return queries.get_run(RUN.id, dependencies=dependencies), repository


def test_current_question_is_read_by_exact_position_and_matches_attempt_snapshot() -> None:
    event = question_event()
    view, repository = read_waiting(event)
    current = view.waiting_input
    assert current.event_id == event.id and current.attempt_id == event.attempt_id
    assert current.generation == event.generation
    assert current.checkpoint_id == view.attempts[0].checkpoint.id
    assert current.waiting_deadline == view.attempts[0].waiting_deadline
    assert current.question.prompt == PROMPT
    repository.event_by_position.assert_called_once_with(
        event.attempt_id, generation=event.generation, sequence=event.sequence
    )
    repository.events_page_by_run_id.assert_not_called()


def test_non_waiting_and_legacy_waiting_have_no_current_question() -> None:
    assert read_waiting(question_event(legacy=True))[0].waiting_input is None
    view, repository = read_waiting(None, attempt=ATTEMPT)
    assert view.waiting_input is None
    repository.event_by_position.assert_not_called()


@pytest.mark.parametrize(
    "case",
    ["missing", "type", "attempt", "generation", "sequence", "checkpoint", "deadline", "question"],
)
def test_missing_or_inconsistent_waiting_evidence_is_unavailable(case: str) -> None:
    event = question_event()
    payload = event.model_dump(mode="python")
    if case == "type":
        payload.update(event_type="ATTEMPT_RUNNING", data={})
    if case == "attempt":
        payload["attempt_id"] = RUN.id
    if case in ("generation", "sequence"):
        payload[case] += 1
    if case == "checkpoint":
        payload["data"]["checkpoint"]["artifact_version"] = "changed"
    if case == "deadline":
        payload["data"]["waitingDeadline"] = NOW.isoformat()
    if case == "question":
        payload["data"]["question"] = {"prompt": "   "}
    malformed = None if case == "missing" else event
    if malformed is not None:
        for field, value in payload.items():
            object.__setattr__(malformed, field, value)
    with pytest.raises(AgentQueryUnavailable):
        read_waiting(malformed)


def test_repository_reads_only_the_exact_attempt_generation_sequence() -> None:
    from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository

    db = Mock()
    db.execute.return_value.mappings.return_value.one_or_none.return_value = None
    assert (
        SqlAlchemyAgentRepository(db).event_by_position(ATTEMPT.id, generation=3, sequence=4)
        is None
    )
    statement, parameters = db.execute.call_args.args
    assert parameters == {"attempt_id": ATTEMPT.id, "generation": 3, "sequence": 4}
    sql = str(statement)
    assert "attempt_id=CAST(:attempt_id AS UUID)" in sql
    assert "runner_generation=:generation AND sequence=:sequence" in sql
    assert "ORDER BY" not in sql


def test_detail_dto_has_required_nullable_projection_and_bounded_strict_question() -> None:
    from control_plane.app.modules.agent.api.dto import (
        AgentRunDetailsResponseDto,
        AgentWaitingInputQuestionResponseDto,
    )

    assert AgentRunDetailsResponseDto.model_fields["waiting_input"].is_required()
    view, _ = read_waiting(question_event())
    payload = AgentRunDetailsResponseDto.from_domain(view).model_dump(mode="json", by_alias=True)
    assert set(payload["waitingInput"]) == {
        "eventId",
        "attemptId",
        "generation",
        "checkpointId",
        "waitingDeadline",
        "question",
    }
    assert payload["waitingInput"]["question"] == {"prompt": PROMPT}
    assert payload["waitingInput"]["waitingDeadline"] == payload["attempts"][0]["waitingDeadline"]
    with pytest.raises(ValueError):
        AgentWaitingInputQuestionResponseDto(prompt=" \n\t")
    with pytest.raises(ValueError):
        AgentWaitingInputQuestionResponseDto.model_validate({"prompt": "valid", "answer": "extra"})
