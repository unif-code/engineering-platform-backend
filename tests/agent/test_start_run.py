from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Barrier, Thread
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import text

from control_plane.app.modules.agent import list_definitions, register_definition, start_run
from control_plane.app.modules.agent.adapters import requirement as requirement_adapter
from control_plane.app.modules.agent.adapters.dev_policy import (
    DevActorResolver,
    DevEventCursorCodec,
    DevExecutionBindingPolicy,
)
from control_plane.app.modules.agent.adapters.requirement import RequirementFacadeExecutionContext
from control_plane.app.modules.agent.adapters.sqlalchemy import (
    SqlAlchemyAgentRepository,
    SqlAlchemyAgentTransactionRunner,
    SqlAlchemyAgentUnitOfWork,
)
from control_plane.app.modules.agent.application.definitions import RegisterDefinitionCommand
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.runs import (
    IdempotencyConflict,
    StartRunCommand,
)
from control_plane.app.modules.agent.domain import (
    AgentAuditAppend,
    AttemptState,
    ExecutionBinding,
    WorkflowCommand,
)
from control_plane.app.modules.agent.ports import AgentUnitOfWork
from control_plane.app.modules.agent.ports.runtime import (
    ExecutionBindingRequest,
    RequirementExecutionContext,
    RequirementExecutionRequest,
)
from tests.agent.conftest import IsolatedAgentDatabase, TestSecretManager
from tests.shared.test_idempotency_contract import MemoryRepository

NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
WORKSPACE_ID = "10000000-0000-0000-0000-000000000901"
REQUIREMENT_ID = "10000000-0000-0000-0000-000000000902"
WORK_ITEM_ID = "10000000-0000-0000-0000-000000000903"
ASSIGNMENT_ID = "10000000-0000-0000-0000-000000000904"
DEFINITION_ID = "00000000-0000-0000-0000-000000000800"
SENSITIVE_SENTINEL = "sensitive-goal-token-never-audit"


@dataclass(frozen=True, slots=True)
class StaticRequirementContext:
    workspace_id: str = WORKSPACE_ID
    assignment_id: str | None = ASSIGNMENT_ID
    superseded: bool = False

    def protect(
        self, command: RequirementExecutionRequest, *, expected_assignment_id: str
    ) -> nullcontext[RequirementExecutionContext]:
        return nullcontext(self.resolve(command))

    def resolve(self, command: RequirementExecutionRequest) -> RequirementExecutionContext:
        if command.workspace_id != self.workspace_id:
            raise ValueError("Requirement workspace does not match Agent workspace")
        if self.superseded or self.assignment_id is None:
            raise ValueError("Requirement WorkItem has no current Agent assignment")
        return RequirementExecutionContext(
            workspace_id=self.workspace_id,
            requirement_id=command.requirement_id,
            work_item_id=command.work_item_id,
            assignment_id=self.assignment_id,
            goal_ref=f"requirement:{command.requirement_id}:work-item:{command.work_item_id}",
        )


@pytest.fixture
def start_application(monkeypatch: pytest.MonkeyPatch) -> tuple[AgentDependencies, Mock, Mock]:
    from control_plane.app.modules.agent.application import idempotency
    from control_plane.app.modules.agent.ports import AgentRepository, AgentTransactionRunner
    from tests.agent.test_repository import DEFINITION

    # Real start and shared sealed-command engine; PostgreSQL tests prove persistence/rollback.
    memory = MemoryRepository()
    monkeypatch.setattr(idempotency, "_SharedRepository", lambda _repository: memory)
    repository = Mock(spec=AgentRepository)
    repository.definition_by_id.return_value = DEFINITION.model_copy(update={"id": DEFINITION_ID})
    for name in ("insert_run", "insert_attempt", "append_event", "insert_workflow_command"):
        getattr(repository, name).side_effect = lambda value: value
    repository.insert_binding.side_effect = lambda _attempt_id, binding: binding
    context = Mock()
    context.resolve.return_value = StaticRequirementContext().resolve(
        RequirementExecutionRequest(
            workspace_id=WORKSPACE_ID, requirement_id=REQUIREMENT_ID, work_item_id=WORK_ITEM_ID
        )
    )
    uow = SimpleNamespace(repository=lambda: repository, append_audit_event=Mock())
    dependencies = AgentDependencies(
        transaction_runner=cast(AgentTransactionRunner, lambda operation: operation(uow)),
        requirement_context=context,
        binding_policy=DevExecutionBindingPolicy(),
        definition_availability=AlwaysActiveDefinitionAvailability(),
        actor_resolver=DevActorResolver(),
        clock=lambda: NOW,
        new_id=DeterministicIds(),
        cursor_codec=DevEventCursorCodec(),
        secret_manager=TestSecretManager(),
    )
    return dependencies, repository, context


def test_start_copies_complete_owner_context_and_replays_without_resolving_it_again(
    start_application: tuple[AgentDependencies, Mock, Mock],
) -> None:
    dependencies, repository, context = start_application
    first = start_run(None, command=_command(), dependencies=dependencies)
    assert first.run.model_dump()["business_context"] == {
        "requirement_id": REQUIREMENT_ID,
        "work_item_id": WORK_ITEM_ID,
        "assignment_id": ASSIGNMENT_ID,
    }
    context.resolve.side_effect = AssertionError(
        "historical replay must not resolve current Assignment"
    )
    replay = start_run(None, command=_command(), dependencies=dependencies)
    assert replay == first
    assert context.resolve.call_count == repository.insert_run.call_count == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_id", "10000000-0000-0000-0000-000000009999"),
        ("requirement_id", "10000000-0000-0000-0000-000000009999"),
        ("work_item_id", "10000000-0000-0000-0000-000000009999"),
        ("assignment_id", None),
        ("assignment_id", ""),
        ("assignment_id", "not-a-uuid"),
    ],
)
def test_start_rejects_incomplete_or_mismatched_owner_context_before_inserting_facts(
    start_application: tuple[AgentDependencies, Mock, Mock],
    field: str,
    value: Any,
) -> None:
    from control_plane.app.modules.agent.application.errors import (
        InvalidRequirementExecutionContext,
    )

    dependencies, repository, context = start_application
    context.resolve.return_value = RequirementExecutionContext.model_construct(
        **{**context.resolve.return_value.model_dump(), field: value}
    )
    with pytest.raises(InvalidRequirementExecutionContext):
        start_run(None, command=_command(), dependencies=dependencies)
    repository.insert_run.assert_not_called()
    repository.insert_workflow_command.assert_not_called()


def test_start_revalidates_an_owner_context_missing_assignment(
    start_application: tuple[AgentDependencies, Mock, Mock],
) -> None:
    from control_plane.app.modules.agent.application.errors import (
        InvalidRequirementExecutionContext,
    )

    dependencies, repository, context = start_application
    context.resolve.return_value = RequirementExecutionContext.model_construct(
        **context.resolve.return_value.model_dump(exclude={"assignment_id"})
    )
    with pytest.raises(InvalidRequirementExecutionContext):
        start_run(None, command=_command(), dependencies=dependencies)
    repository.insert_run.assert_not_called()


class DeterministicIds:
    def __init__(self) -> None:
        self._next = 910

    def __call__(self) -> str:
        result = str(UUID(int=self._next))
        self._next += 1
        return result


class FailingBindingPolicy:
    def resolve(self, _request: ExecutionBindingRequest) -> ExecutionBinding:
        raise RuntimeError("binding policy unavailable")


class InactiveDefinitionAvailability:
    def is_active(self, _definition: object) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class RequirementBoundary:
    id: str
    workspace_id: str


@dataclass(frozen=True, slots=True)
class WorkItemBoundary:
    id: str
    requirement_id: str


@dataclass(frozen=True, slots=True)
class AssignmentBoundary:
    id: str
    work_item_id: str
    superseded_at: datetime | None


@dataclass(frozen=True, slots=True)
class RequirementDetailsBoundary:
    requirement: RequirementBoundary
    work_items: tuple[WorkItemBoundary, ...]
    work_item_assignments: tuple[AssignmentBoundary, ...]


class AuditFailingUnitOfWork:
    def __init__(self, delegate: SqlAlchemyAgentUnitOfWork) -> None:
        self._delegate = delegate

    def repository(self) -> SqlAlchemyAgentRepository:
        return self._delegate.repository()

    def append_audit_event(self, _event: AgentAuditAppend) -> None:
        raise RuntimeError("audit append unavailable")


class AuditFailingTransactionRunner:
    def __init__(self, database: IsolatedAgentDatabase) -> None:
        self._delegate = SqlAlchemyAgentTransactionRunner(database.runtime)

    def __call__[T](self, operation: Callable[[AgentUnitOfWork], T]) -> T:
        return self._delegate(lambda uow: operation(AuditFailingUnitOfWork(uow)))


class PersistenceFailingRepository(SqlAlchemyAgentRepository):
    def insert_workflow_command(self, _command: WorkflowCommand) -> WorkflowCommand:
        raise RuntimeError("workflow command persistence unavailable")


class PersistenceFailingUnitOfWork:
    def __init__(self, delegate: SqlAlchemyAgentUnitOfWork) -> None:
        self._delegate = delegate

    def repository(self) -> PersistenceFailingRepository:
        return PersistenceFailingRepository(self._delegate.repository().db)

    def append_audit_event(self, event: AgentAuditAppend) -> None:
        self._delegate.append_audit_event(event)


class PersistenceFailingTransactionRunner:
    def __init__(self, database: IsolatedAgentDatabase) -> None:
        self._delegate = SqlAlchemyAgentTransactionRunner(database.runtime)

    def __call__[T](self, operation: Callable[[AgentUnitOfWork], T]) -> T:
        return self._delegate(lambda uow: operation(PersistenceFailingUnitOfWork(uow)))


def _command(
    *, goal: str = "Create a governed probe", actor: str = "employee-901"
) -> StartRunCommand:
    return StartRunCommand(
        workspace_id=WORKSPACE_ID,
        requirement_id=REQUIREMENT_ID,
        work_item_id=WORK_ITEM_ID,
        definition_id=DEFINITION_ID,
        definition_version=1,
        goal=goal,
        actor=actor,
        idempotency_key="start-run-901",
        correlation_id="correlation-901",
    )


def _dependencies(database: IsolatedAgentDatabase) -> AgentDependencies:
    return AgentDependencies(
        transaction_runner=SqlAlchemyAgentTransactionRunner(database.runtime),
        requirement_context=StaticRequirementContext(),
        binding_policy=DevExecutionBindingPolicy(),
        definition_availability=AlwaysActiveDefinitionAvailability(),
        actor_resolver=DevActorResolver(),
        clock=lambda: NOW,
        new_id=DeterministicIds(),
        cursor_codec=DevEventCursorCodec(),
        secret_manager=TestSecretManager(),
    )


class AlwaysActiveDefinitionAvailability:
    def is_active(self, _definition: object) -> bool:
        return True


def _requirement_details(
    *,
    workspace_id: str = WORKSPACE_ID,
    work_item_id: str = WORK_ITEM_ID,
    work_item_requirement_id: str = REQUIREMENT_ID,
    assignments: tuple[AssignmentBoundary, ...] = (),
) -> RequirementDetailsBoundary:
    return RequirementDetailsBoundary(
        requirement=RequirementBoundary(id=REQUIREMENT_ID, workspace_id=workspace_id),
        work_items=(WorkItemBoundary(id=work_item_id, requirement_id=work_item_requirement_id),),
        work_item_assignments=assignments,
    )


def _assignment(
    *, assignment_id: str = ASSIGNMENT_ID, superseded: bool = False
) -> AssignmentBoundary:
    return AssignmentBoundary(
        id=assignment_id,
        work_item_id=WORK_ITEM_ID,
        superseded_at=NOW if superseded else None,
    )


def _count(database: IsolatedAgentDatabase, table: str) -> int:
    with database.owner.connect() as db:
        return int(db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def _agent_audit_count(database: IsolatedAgentDatabase) -> int:
    with database.owner.connect() as db:
        return int(
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE action LIKE 'agent.%'")
            ).scalar_one()
        )


def test_start_persists_one_queued_attempt_binding_command_and_safe_audit(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = _dependencies(isolated_agent_database)

    result = start_run(None, command=_command(), dependencies=dependencies)

    assert result.run.business_context is not None
    assert result.run.business_context.model_dump() == {
        "requirement_id": REQUIREMENT_ID,
        "work_item_id": WORK_ITEM_ID,
        "assignment_id": ASSIGNMENT_ID,
    }
    with isolated_agent_database.runtime.connect() as db:
        assert SqlAlchemyAgentRepository(db).run_by_id(result.run.id) == result.run
    assert result.attempt.state is AttemptState.QUEUED
    assert result.binding.source == "DEV_FAKE"
    assert result.binding.runtime_permissions == (
        "checkpoint.write",
        "context.read",
        "event.emit",
    )
    assert result.command.command_key == f"start:{result.attempt.id}:generation-1"
    assert _count(isolated_agent_database, "agent.agent_run") == 1
    assert _count(isolated_agent_database, "agent.agent_attempt") == 1
    assert _count(isolated_agent_database, "agent.execution_binding") == 1
    assert _count(isolated_agent_database, "agent.canonical_event") == 1
    assert _count(isolated_agent_database, "agent.workflow_command") == 1
    with isolated_agent_database.owner.connect() as db:
        audit = db.execute(
            text(
                "SELECT action, reason FROM audit.audit_event "
                "WHERE action='agent.run.start' ORDER BY occurred_at, id"
            )
        ).one()
    assert audit.action == "agent.run.start"
    assert _command().goal not in audit.reason
    assert "DEV_FAKE" in audit.reason


def test_audit_never_persists_caller_goal_or_request_secret_sentinel(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    command = _command(goal=SENSITIVE_SENTINEL).model_copy(
        update={
            "correlation_id": SENSITIVE_SENTINEL,
            "idempotency_key": SENSITIVE_SENTINEL,
        }
    )

    start_run(None, command=command, dependencies=_dependencies(isolated_agent_database))

    with isolated_agent_database.owner.connect() as db:
        evidence = (
            db.execute(
                text(
                    "SELECT id, occurred_at, actor, actor_type, action, target_type, target_id, "
                    "result, reason, correlation_id, request_id, schema_version "
                    "FROM audit.audit_event WHERE action='agent.run.start'"
                )
            )
            .mappings()
            .one()
        )
    assert all(SENSITIVE_SENTINEL not in str(value) for value in evidence.values())


def test_start_rejects_valid_looking_sensitive_actor_before_any_platform_fact(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with pytest.raises(ValueError, match="trusted platform actor"):
        start_run(
            None,
            command=_command(actor=f"employee-{SENSITIVE_SENTINEL}"),
            dependencies=_dependencies(isolated_agent_database),
        )

    assert _agent_audit_count(isolated_agent_database) == 0
    assert _count(isolated_agent_database, "agent.agent_run") == 0


def test_start_revalidates_actor_copied_past_command_validation(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    command = _command().model_copy(update={"actor": SENSITIVE_SENTINEL})

    with pytest.raises(ValueError, match="trusted platform actor"):
        start_run(None, command=command, dependencies=_dependencies(isolated_agent_database))

    assert _agent_audit_count(isolated_agent_database) == 0
    assert _count(isolated_agent_database, "agent.agent_run") == 0


@pytest.mark.parametrize(
    ("actor", "actor_type"),
    [
        ("employee-901", "EMPLOYEE"),
        ("service-901", "SERVICE"),
        ("system-901", "SYSTEM"),
    ],
)
def test_start_persists_resolved_actor_and_audit_type(
    isolated_agent_database: IsolatedAgentDatabase,
    actor: str,
    actor_type: str,
) -> None:
    result = start_run(
        None,
        command=_command(actor=actor),
        dependencies=_dependencies(isolated_agent_database),
    )

    with isolated_agent_database.owner.connect() as db:
        audit = db.execute(
            text("SELECT actor, actor_type FROM audit.audit_event WHERE action='agent.run.start'")
        ).one()
    assert result.run.created_by == actor
    assert audit == (actor, actor_type)


def test_start_scopes_idempotency_to_the_resolved_actor(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = _dependencies(isolated_agent_database)
    employee = start_run(
        None,
        command=_command(actor="employee-901"),
        dependencies=dependencies,
    )
    service = start_run(
        None,
        command=_command(actor="service-901"),
        dependencies=dependencies,
    )

    assert employee.run.id != service.run.id
    assert employee.run.created_by == "employee-901"
    assert service.run.created_by == "service-901"
    assert _count(isolated_agent_database, "agent.idempotency_key") == 2


def test_register_definition_rejects_valid_looking_sensitive_actor_before_audit(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with pytest.raises(ValueError, match="trusted platform actor"):
        register_definition(
            None,
            command=RegisterDefinitionCommand(
                id=DEFINITION_ID,
                version=2,
                name="agent/dev-control-plane-probe",
                capability_declarations=("agent.run.execute",),
                skill_declarations=("writing-plans",),
                runtime_permissions=("checkpoint.write", "context.read", "event.emit"),
                input_schema={"type": "object"},
                actor=f"employee-{SENSITIVE_SENTINEL}",
                correlation_id=SENSITIVE_SENTINEL,
            ),
            dependencies=_dependencies(isolated_agent_database),
        )

    assert _agent_audit_count(isolated_agent_database) == 0
    assert _count(isolated_agent_database, "agent.agent_definition") == 1


@pytest.mark.parametrize(
    ("actor", "actor_type"),
    [
        ("employee-901", "EMPLOYEE"),
        ("service-901", "SERVICE"),
        ("system-901", "SYSTEM"),
    ],
)
def test_register_definition_persists_resolved_actor_and_audit_type(
    isolated_agent_database: IsolatedAgentDatabase,
    actor: str,
    actor_type: str,
) -> None:
    register_definition(
        None,
        command=RegisterDefinitionCommand(
            id=DEFINITION_ID,
            version=2,
            name="agent/dev-control-plane-probe",
            capability_declarations=("agent.run.execute",),
            skill_declarations=("writing-plans",),
            runtime_permissions=("checkpoint.write", "context.read", "event.emit"),
            input_schema={"type": "object"},
            actor=actor,
            correlation_id="definition-901",
        ),
        dependencies=_dependencies(isolated_agent_database),
    )

    with isolated_agent_database.owner.connect() as db:
        audit = db.execute(
            text(
                "SELECT actor, actor_type FROM audit.audit_event "
                "WHERE action='agent.definition.register'"
            )
        ).one()
    assert audit == (actor, actor_type)


def test_same_key_replays_exact_result_and_changed_body_conflicts(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = _dependencies(isolated_agent_database)
    first = start_run(None, command=_command(), dependencies=dependencies)

    replay = start_run(None, command=_command(), dependencies=dependencies)

    assert replay == first
    assert _count(isolated_agent_database, "agent.agent_run") == 1
    assert _agent_audit_count(isolated_agent_database) == 1
    with pytest.raises(IdempotencyConflict):
        start_run(None, command=_command(goal="Changed goal body"), dependencies=dependencies)
    assert _count(isolated_agent_database, "agent.workflow_command") == 1


def test_same_business_request_replays_when_only_correlation_changes(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = _dependencies(isolated_agent_database)
    first = start_run(None, command=_command(), dependencies=dependencies)

    replay = start_run(
        None,
        command=_command().model_copy(update={"correlation_id": "correlation-retry-901"}),
        dependencies=dependencies,
    )

    assert replay == first
    assert _count(isolated_agent_database, "agent.agent_run") == 1
    assert _agent_audit_count(isolated_agent_database) == 1


def test_context_failure_leaves_no_agent_or_audit_fact(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = _dependencies(isolated_agent_database)
    dependencies = dependencies.with_requirement_context(
        StaticRequirementContext(assignment_id=None)
    )

    with pytest.raises(ValueError, match="current Agent assignment"):
        start_run(None, command=_command(), dependencies=dependencies)

    assert _count(isolated_agent_database, "agent.agent_run") == 0
    assert _count(isolated_agent_database, "agent.workflow_command") == 0
    assert _agent_audit_count(isolated_agent_database) == 0


def test_inactive_definition_does_not_reserve_or_dispatch(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = _dependencies(isolated_agent_database)

    with pytest.raises(ValueError, match="active Agent Definition"):
        start_run(
            None,
            command=_command().model_copy(update={"definition_version": 2}),
            dependencies=dependencies,
        )

    assert _count(isolated_agent_database, "agent.idempotency_key") == 0
    assert _count(isolated_agent_database, "agent.workflow_command") == 0


def test_existing_inactive_definition_is_rejected_and_hidden_from_listing(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = replace(
        _dependencies(isolated_agent_database),
        definition_availability=InactiveDefinitionAvailability(),
    )

    with pytest.raises(ValueError, match="inactive Agent Definition"):
        start_run(None, command=_command(), dependencies=dependencies)

    assert list_definitions(None, dependencies=dependencies) == ()
    assert _count(isolated_agent_database, "agent.idempotency_key") == 0
    assert _count(isolated_agent_database, "agent.workflow_command") == 0


@pytest.mark.parametrize(
    ("details", "message"),
    [
        (
            _requirement_details(
                workspace_id="10000000-0000-0000-0000-000000009998", assignments=(_assignment(),)
            ),
            "workspace",
        ),
        (
            _requirement_details(work_item_id="10000000-0000-0000-0000-000000009998"),
            "missing or ambiguous",
        ),
        (
            _requirement_details(
                work_item_requirement_id="10000000-0000-0000-0000-000000009998",
                assignments=(_assignment(),),
            ),
            "does not belong",
        ),
        (
            _requirement_details(
                assignments=(
                    _assignment(),
                    _assignment(assignment_id="10000000-0000-0000-0000-000000000905"),
                )
            ),
            "no current",
        ),
        (_requirement_details(assignments=(_assignment(superseded=True),)), "no current"),
    ],
)
def test_requirement_facade_rejects_invalid_current_context(
    monkeypatch: pytest.MonkeyPatch,
    details: RequirementDetailsBoundary,
    message: str,
) -> None:
    monkeypatch.setattr(requirement_adapter, "get_requirement", lambda *_args, **_kwargs: details)
    adapter = RequirementFacadeExecutionContext(None, None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=message):
        adapter.resolve(
            RequirementExecutionRequest(
                workspace_id=WORKSPACE_ID,
                requirement_id=REQUIREMENT_ID,
                work_item_id=WORK_ITEM_ID,
            )
        )


def test_requirement_facade_returns_exactly_one_current_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requirement_adapter,
        "get_requirement",
        lambda *_args, **_kwargs: _requirement_details(assignments=(_assignment(),)),
    )
    adapter = RequirementFacadeExecutionContext(None, None)  # type: ignore[arg-type]

    assert (
        adapter.resolve(
            RequirementExecutionRequest(
                workspace_id=WORKSPACE_ID,
                requirement_id=REQUIREMENT_ID,
                work_item_id=WORK_ITEM_ID,
            )
        ).assignment_id
        == ASSIGNMENT_ID
    )


def test_binding_policy_failure_rolls_back_before_reserving_or_dispatching(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = replace(
        _dependencies(isolated_agent_database), binding_policy=FailingBindingPolicy()
    )

    with pytest.raises(RuntimeError, match="binding policy unavailable"):
        start_run(None, command=_command(), dependencies=dependencies)

    assert _count(isolated_agent_database, "agent.idempotency_key") == 0
    assert _count(isolated_agent_database, "agent.workflow_command") == 0
    assert _agent_audit_count(isolated_agent_database) == 0


def test_exact_replay_uses_sealed_result_without_reresolving_binding_policy(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = _dependencies(isolated_agent_database)
    first = start_run(None, command=_command(), dependencies=dependencies)

    replay = start_run(
        None,
        command=_command(),
        dependencies=replace(dependencies, binding_policy=FailingBindingPolicy()),
    )

    assert replay == first
    assert _count(isolated_agent_database, "agent.agent_run") == 1
    assert _count(isolated_agent_database, "agent.workflow_command") == 1


def test_audit_failure_rolls_back_all_agent_facts_and_command(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = replace(
        _dependencies(isolated_agent_database),
        transaction_runner=AuditFailingTransactionRunner(isolated_agent_database),
    )

    with pytest.raises(RuntimeError, match="audit append unavailable"):
        start_run(None, command=_command(), dependencies=dependencies)

    for table in (
        "agent.agent_run",
        "agent.agent_attempt",
        "agent.execution_binding",
        "agent.canonical_event",
        "agent.workflow_command",
        "agent.idempotency_key",
    ):
        assert _count(isolated_agent_database, table) == 0
    assert _agent_audit_count(isolated_agent_database) == 0


def test_real_transaction_rolls_back_when_workflow_command_persistence_fails(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = replace(
        _dependencies(isolated_agent_database),
        transaction_runner=PersistenceFailingTransactionRunner(isolated_agent_database),
    )

    with pytest.raises(RuntimeError, match="workflow command persistence unavailable"):
        start_run(None, command=_command(), dependencies=dependencies)

    for table in (
        "agent.agent_run",
        "agent.agent_attempt",
        "agent.execution_binding",
        "agent.canonical_event",
        "agent.workflow_command",
        "agent.idempotency_key",
    ):
        assert _count(isolated_agent_database, table) == 0
    assert _agent_audit_count(isolated_agent_database) == 0


def test_concurrent_same_key_start_converges_on_one_sealed_result(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    barrier = Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def start() -> None:
        try:
            dependencies = _dependencies(isolated_agent_database)
            barrier.wait(timeout=5)
            results.append(start_run(None, command=_command(), dependencies=dependencies))
        except BaseException as error:  # pragma: no cover - asserted by parent thread
            errors.append(error)

    first = Thread(target=start)
    second = Thread(target=start)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert _count(isolated_agent_database, "agent.agent_run") == 1
    assert _count(isolated_agent_database, "agent.workflow_command") == 1
    assert _agent_audit_count(isolated_agent_database) == 1


def test_register_definition_creates_an_immutable_new_version(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    dependencies = _dependencies(isolated_agent_database)
    created = register_definition(
        None,
        command=RegisterDefinitionCommand(
            id=DEFINITION_ID,
            version=2,
            name="agent/dev-control-plane-probe",
            capability_declarations=("agent.run.execute",),
            skill_declarations=("writing-plans",),
            runtime_permissions=("checkpoint.write", "context.read", "event.emit"),
            input_schema={"type": "object"},
            actor="employee-901",
            correlation_id="definition-901",
        ),
        dependencies=dependencies,
    )

    assert created.version == 2
    assert [item.version for item in list_definitions(None, dependencies=dependencies)] == [1, 2]
    assert _agent_audit_count(isolated_agent_database) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_id", "10000000-0000-0000-0000-000000009999"),
        ("requirement_id", "10000000-0000-0000-0000-000000009999"),
        ("work_item_id", "10000000-0000-0000-0000-000000009999"),
        ("assignment_id", None),
        ("assignment_id", "not-a-uuid"),
    ],
)
def test_invalid_owner_source_rolls_back_the_real_start_transaction(
    isolated_agent_database: IsolatedAgentDatabase,
    field: str,
    value: Any,
) -> None:
    from control_plane.app.modules.agent.application.errors import (
        InvalidRequirementExecutionContext,
    )
    from tests.agent.test_final_integrity import facts

    context = Mock()
    context.resolve.return_value = RequirementExecutionContext.model_construct(
        **{
            "workspace_id": WORKSPACE_ID,
            "requirement_id": REQUIREMENT_ID,
            "work_item_id": WORK_ITEM_ID,
            "assignment_id": ASSIGNMENT_ID,
            "goal_ref": "goal:opaque-unparsed",
            field: value,
        }
    )
    dependencies = replace(_dependencies(isolated_agent_database), requirement_context=context)
    before = facts(isolated_agent_database)
    with pytest.raises(InvalidRequirementExecutionContext):
        start_run(None, command=_command(), dependencies=dependencies)
    assert facts(isolated_agent_database) == before
