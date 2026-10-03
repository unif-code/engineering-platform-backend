from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest

from control_plane.app.modules import requirement as requirement_facade
from control_plane.app.modules.agent.adapters import requirement as requirement_adapter
from control_plane.app.modules.agent.application.control import (
    AttemptRevisionConflict,
    AttemptWaitingExpired,
    BindingDigestMismatch,
    ResumeAttemptCommand,
    resume_attempt,
)
from control_plane.app.modules.agent.application.errors import (
    AgentBusinessContextReason,
    InvalidRequirementExecutionContext,
)
from control_plane.app.modules.agent.application.runs import IdempotencyConflict
from control_plane.app.modules.agent.domain import AttemptNotResumable, AttemptState
from control_plane.app.modules.agent.ports.runtime import RequirementExecutionContext
from control_plane.app.modules.requirement import (
    RequirementDependencyUnavailable,
    RequirementNotFound,
)
from tests.agent.test_repository import ATTEMPT, BINDING, CHECKPOINT, NOW, RUN
from tests.agent.test_start_run import _assignment, _requirement_details
from tests.agent.test_start_run import start_application as start_application


@pytest.fixture
def resume_application(start_application: Any) -> SimpleNamespace:
    dependencies, repository, port = start_application
    timeline: list[str] = []
    uow = Mock()
    uow.repository.return_value = repository
    waiting = ATTEMPT.model_copy(
        update={
            "state": AttemptState.WAITING_INPUT,
            "checkpoint": CHECKPOINT,
            "waiting_deadline": NOW + timedelta(minutes=5),
        }
    )
    repository.run_by_id.return_value = RUN
    repository.attempt_by_id.return_value = waiting
    repository.binding_by_attempt_id.return_value = BINDING
    assert RUN.business_context is not None
    current = RequirementExecutionContext(
        workspace_id=RUN.workspace_id,
        **RUN.business_context.model_dump(),
        goal_ref="opaque:current-owner-reference",
    )
    harness = SimpleNamespace(
        repository=repository,
        port=port,
        uow=uow,
        waiting=waiting,
        current=current,
        now=NOW,
        timeline=timeline,
        failure=None,
    )

    @contextmanager
    def protect(_request: Any) -> Iterator[RequirementExecutionContext]:
        timeline.append("owner-lock")
        try:
            yield harness.current
        finally:
            timeline.append("owner-release")

    port.protect.side_effect = protect
    port.resolve.side_effect = AssertionError("resume must use owner protection")

    def persist(_attempt_id: str, *, expected_revision: int, mutation: Any) -> Any:
        assert timeline[-1] == "owner-lock"
        result = waiting.model_copy(
            update={**mutation.model_dump(exclude={"now"}), "revision": expected_revision + 1}
        )
        repository.attempt_by_id.return_value = result
        return result

    repository.compare_and_set_attempt.side_effect = persist

    def transaction(operation: Any) -> Any:
        try:
            result = operation(uow)
            if harness.failure:
                raise harness.failure
        except BaseException:
            timeline.append("agent-rollback")
            raise
        timeline.append("agent-commit")
        return result

    harness.dependencies = replace(
        dependencies, transaction_runner=transaction, clock=lambda: harness.now
    )
    harness.command = ResumeAttemptCommand(
        run_id=RUN.id,
        attempt_id=ATTEMPT.id,
        expected_revision=ATTEMPT.revision,
        actor="employee-901",
        idempotency_key="protected-resume",
        correlation_id="synthetic-resume",
    )
    return harness


def execute(harness: SimpleNamespace) -> Any:
    return resume_attempt(harness.command, dependencies=harness.dependencies)


def test_owner_protection_spans_actual_commit_and_replay_never_reopens_it(
    resume_application: SimpleNamespace,
) -> None:
    h = resume_application
    result = execute(h)
    assert h.timeline == ["owner-lock", "agent-commit", "owner-release"]
    assert result.attempt.id == ATTEMPT.id and result.attempt.binding_id == BINDING.id
    assert result.attempt.checkpoint == CHECKPOINT
    assert result.attempt.runner_generation == ATTEMPT.runner_generation + 1
    assert result.attempt.state is AttemptState.QUEUED
    assert RUN.business_context is not None
    request = h.port.protect.call_args.args[0]
    assert request.model_dump() == {
        "workspace_id": RUN.workspace_id,
        "requirement_id": RUN.business_context.requirement_id,
        "work_item_id": RUN.business_context.work_item_id,
    }
    h.port.protect.side_effect = AssertionError("replay must not acquire owner")
    h.now += timedelta(days=1)
    h.repository.run_by_id.return_value = RUN.model_copy(update={"business_context": None})
    assert execute(h) == result
    h.command = h.command.model_copy(update={"expected_revision": result.attempt.revision})
    with pytest.raises(IdempotencyConflict):
        execute(h)
    assert h.port.protect.call_count == 1
    h.repository.compare_and_set_attempt.assert_called_once()
    h.repository.insert_workflow_command.assert_called_once()
    h.uow.append_audit_event.assert_called_once()


@pytest.mark.parametrize("case", ["source", "revision", "state", "deadline", "binding"])
def test_local_resume_rejections_do_not_need_the_owner(
    resume_application: SimpleNamespace, case: str
) -> None:
    h = resume_application
    expected: type[Exception] = AttemptNotResumable
    if case == "source":
        h.repository.run_by_id.return_value = RUN.model_copy(update={"business_context": None})
    if case == "revision":
        h.command = h.command.model_copy(update={"expected_revision": 99})
        expected = AttemptRevisionConflict
    if case in ("state", "deadline"):
        updates = {
            "state": {"state": AttemptState.CANCELED},
            "deadline": {"waiting_deadline": NOW},
        }
        h.repository.attempt_by_id.return_value = h.waiting.model_copy(update=updates[case])
        if case == "deadline":
            expected = AttemptWaitingExpired
    if case == "binding":
        h.repository.binding_by_attempt_id.return_value = None
        expected = BindingDigestMismatch
    with pytest.raises(expected):
        execute(h)
    h.port.protect.assert_not_called()
    h.repository.compare_and_set_attempt.assert_not_called()
    h.repository.insert_workflow_command.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("workspace_id", str(UUID(int=99)), AttemptNotResumable),
        ("assignment_id", str(UUID(int=99)), AttemptNotResumable),
        ("requirement_id", str(UUID(int=99)), RequirementDependencyUnavailable),
        ("work_item_id", str(UUID(int=99)), RequirementDependencyUnavailable),
        ("assignment_id", "invalid-private-source", RequirementDependencyUnavailable),
        ("workspace_id", "invalid-private-workspace", RequirementDependencyUnavailable),
    ],
)
def test_protected_owner_identity_must_be_valid_and_match_the_saved_source(
    resume_application: SimpleNamespace, field: str, value: str, expected: type[Exception]
) -> None:
    h = resume_application
    h.current = h.current.model_copy(update={field: value})
    with pytest.raises(expected):
        execute(h)
    assert h.timeline == ["owner-lock", "agent-rollback", "owner-release"]
    h.repository.compare_and_set_attempt.assert_not_called()
    h.uow.append_audit_event.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RequirementNotFound("missing"), AttemptNotResumable),
        *[
            (
                InvalidRequirementExecutionContext("opaque", reason=reason),
                AttemptNotResumable,
            )
            for reason in (
                AgentBusinessContextReason.WORKSPACE_CHANGED,
                AgentBusinessContextReason.WORK_ITEM_NOT_IN_REQUIREMENT,
                AgentBusinessContextReason.ASSIGNMENT_MISSING,
            )
        ],
        *[
            (
                InvalidRequirementExecutionContext("opaque", reason=reason),
                RequirementDependencyUnavailable,
            )
            for reason in (
                AgentBusinessContextReason.OWNER_DATA_INVALID,
                AgentBusinessContextReason.OWNER_DATA_AMBIGUOUS,
                None,
            )
        ],
        (RequirementDependencyUnavailable("opaque"), RequirementDependencyUnavailable),
    ],
)
def test_owner_refusal_is_classified_without_changing_start_or_get_errors(
    resume_application: SimpleNamespace, error: Exception, expected: type[Exception]
) -> None:
    h = resume_application
    h.port.protect.side_effect = error
    with pytest.raises(expected):
        execute(h)
    h.repository.compare_and_set_attempt.assert_not_called()
    h.uow.append_audit_event.assert_not_called()


def test_owner_lock_wait_cannot_extend_the_waiting_deadline(
    resume_application: SimpleNamespace,
) -> None:
    h = resume_application
    acquire = h.port.protect.side_effect

    def wait_for_owner(request: Any) -> Any:
        h.now = h.waiting.waiting_deadline
        return acquire(request)

    h.port.protect.side_effect = wait_for_owner
    with pytest.raises(AttemptWaitingExpired):
        execute(h)
    assert h.timeline == ["owner-lock", "agent-rollback", "owner-release"]
    h.repository.compare_and_set_attempt.assert_not_called()


def test_owner_protection_is_released_after_transaction_runner_failure(
    resume_application: SimpleNamespace,
) -> None:
    h = resume_application
    h.failure = RuntimeError("synthetic commit failure")
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        execute(h)
    assert h.timeline == ["owner-lock", "agent-rollback", "owner-release"]


def test_protected_adapter_uses_public_parent_lock_until_context_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.app.modules.agent.ports.runtime import RequirementExecutionRequest
    from tests.agent.test_start_run import REQUIREMENT_ID, WORK_ITEM_ID, WORKSPACE_ID

    connection = MagicMock()
    details = _requirement_details(assignments=(_assignment(),))
    ordinary = Mock(return_value=details)
    protected = Mock(return_value=details)
    monkeypatch.setattr(requirement_adapter, "get_requirement", ordinary)
    monkeypatch.setattr(requirement_adapter, "get_requirement_for_update", protected)
    adapter = requirement_adapter.RequirementFacadeExecutionContext(connection, Mock())
    request = RequirementExecutionRequest(
        workspace_id=WORKSPACE_ID, requirement_id=REQUIREMENT_ID, work_item_id=WORK_ITEM_ID
    )
    original = adapter.resolve(request)
    connection.begin.assert_not_called()
    protected.assert_not_called()
    with adapter.protect(request) as current:
        assert current == original
        connection.begin.return_value.__enter__.assert_called_once()
        connection.begin.return_value.__exit__.assert_not_called()
        protected.assert_called_once()
    connection.begin.return_value.__exit__.assert_called_once()
    ordinary.assert_called_once()


def test_public_protected_read_passes_parent_lock_flag_to_owner_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = Mock()
    monkeypatch.setattr(requirement_facade, "_get_requirement", query)
    repository = Mock()
    dependencies = Mock(repository_factory=lambda _db: repository)
    requirement_facade.get_requirement_for_update(
        Mock(), requirement_id=RUN.id, dependencies=dependencies
    )
    query.assert_called_once_with(repository, requirement_id=RUN.id, for_update=True)
