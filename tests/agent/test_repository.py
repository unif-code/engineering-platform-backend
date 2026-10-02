from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, Thread
from unittest.mock import Mock

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from control_plane.app.modules.agent.adapters.sqlalchemy import (
    SqlAlchemyAgentRepository,
    SqlAlchemyAgentTransactionRunner,
)
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentAuditAppend,
    AgentDefinition,
    AgentIdempotencyRecord,
    AgentQueryUnavailable,
    AgentRun,
    AgentRunBusinessContext,
    AttemptMutation,
    AttemptState,
    CanonicalEventInput,
    CheckpointInput,
    ExecutionBinding,
    ExecutionBindingSource,
    IdempotencyCompletion,
    IdempotencyState,
    RunMutation,
    RunState,
    WorkflowClaimMode,
    WorkflowCommand,
    WorkflowCommandKind,
    WorkflowCommandState,
    WorkflowDispatchMutation,
)
from control_plane.app.modules.agent.domain.errors import EventReplayConflict
from control_plane.app.modules.agent.ports import AgentUnitOfWork
from tests.agent.conftest import IsolatedAgentDatabase

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
DEFINITION = AgentDefinition(
    id="10000000-0000-0000-0000-000000000811",
    version=1,
    name="agent/test-repository",
    capability_declarations=("agent.run.execute",),
    skill_declarations=("writing-plans",),
    runtime_permissions=("context.read", "event.emit"),
    input_schema={"type": "object", "properties": {"goal": {"type": "string"}}},
    created_at=NOW,
)
BINDING = ExecutionBinding(
    id="10000000-0000-0000-0000-000000000812",
    source=ExecutionBindingSource.DEV_FAKE,
    runtime_ref="dev-runtime-v1",
    model_route_ref="dev-model-route-v1",
    capability_bundle_ref="dev-capability-bundle-v1",
    skill_refs=("writing-plans",),
    runtime_permissions=("context.read", "event.emit"),
    context_policy_ref="dev-context-policy-v1",
    network_policy_ref="dev-network-policy-none",
)
ATTEMPT = AgentAttempt(
    id="10000000-0000-0000-0000-000000000813",
    run_id="10000000-0000-0000-0000-000000000814",
    number=1,
    state=AttemptState.QUEUED,
    binding_id=BINDING.id,
    binding_digest=BINDING.digest,
    runner_generation=1,
    fencing_token="fence-1",
    checkpoint=None,
    revision=1,
    created_at=NOW,
    updated_at=NOW,
)
RUN = AgentRun(
    id=ATTEMPT.run_id,
    workspace_id="10000000-0000-0000-0000-000000000815",
    business_context=AgentRunBusinessContext(
        requirement_id="10000000-0000-0000-0000-000000000816",
        work_item_id="10000000-0000-0000-0000-000000000817",
        assignment_id="10000000-0000-0000-0000-000000000818",
    ),
    goal_ref="goal:immutable-repository-test",
    created_by="employee-815",
    definition_id=DEFINITION.id,
    definition_version=DEFINITION.version,
    latest_attempt_id=ATTEMPT.id,
    state=RunState.ACTIVE,
    revision=1,
    created_at=NOW,
    updated_at=NOW,
)
EVENT = CanonicalEventInput(
    id="10000000-0000-0000-0000-000000000816",
    event_type="ATTEMPT_QUEUED",
    attempt_id=ATTEMPT.id,
    generation=1,
    sequence=1,
    correlation_id="correlation-816",
    causation_id=None,
    trace_id="trace-816",
    span_id="span-816",
    summary="Queued without side effects",
    data={"bindingSource": "DEV_FAKE", "bindingDigest": "a" * 64},
)
COMMAND = WorkflowCommand(
    id="10000000-0000-0000-0000-000000000817",
    command_key="start:attempt-813:generation-1",
    kind=WorkflowCommandKind.START,
    attempt_id=ATTEMPT.id,
    generation=1,
    state=WorkflowCommandState.PLANNED,
    created_at=NOW,
    updated_at=NOW,
)
CHECKPOINT = CheckpointInput(
    id="10000000-0000-0000-0000-000000000818",
    artifact_id="artifact:checkpoint-818",
    artifact_version="1",
    content_sha256="sha256:" + "a" * 64,
    schema_version="1",
    adapter_version="dev-v1",
    classification="INTERNAL",
)


def _seed(repository: SqlAlchemyAgentRepository) -> None:
    assert repository.insert_definition(DEFINITION) == DEFINITION
    assert repository.insert_run(RUN) == RUN
    assert repository.insert_attempt(ATTEMPT) == ATTEMPT
    assert repository.insert_binding(ATTEMPT.id, BINDING) == BINDING


def insert_predecessor_run(db: Connection, run: AgentRun = RUN) -> None:
    """Seed the pre-0004 contract explicitly, without a runtime compatibility path."""
    db.execute(
        text(
            "INSERT INTO agent.agent_run (id, workspace_id, goal_ref, created_by, definition_id, "
            "definition_version, latest_attempt_id, state, revision, created_at, updated_at) "
            "VALUES (:id, :workspace_id, :goal_ref, :created_by, :definition_id, "
            ":definition_version, :latest_attempt_id, :state, :revision, :created_at, :updated_at)"
        ),
        run.model_dump(mode="json", exclude={"business_context"}),
    )


def test_new_run_insert_rejects_missing_source_before_sql() -> None:
    db = Mock()
    with pytest.raises(ValueError, match="complete business context"):
        SqlAlchemyAgentRepository(db).insert_run(RUN.model_copy(update={"business_context": None}))
    db.execute.assert_not_called()


@pytest.mark.parametrize("missing", [None, "assignment_id", "work_item_id", "requirement_id"])
def test_run_storage_maps_explicit_null_but_rejects_partial_source(missing: str | None) -> None:
    row = RUN.model_dump(exclude={"business_context"}) | {
        "requirement_id": None,
        "work_item_id": None,
        "assignment_id": None,
    }
    assert SqlAlchemyAgentRepository._run(row).business_context is None
    assert RUN.business_context is not None
    row.update(RUN.business_context.model_dump())
    if missing is None:
        assert SqlAlchemyAgentRepository._run(row) == RUN
    else:
        row[missing] = None
        with pytest.raises(AgentQueryUnavailable):
            SqlAlchemyAgentRepository._run(row)
        del row[missing]
        with pytest.raises(AgentQueryUnavailable):
            SqlAlchemyAgentRepository._run(row)


def test_repository_port_contains_only_platform_dtos_and_uow_seams() -> None:
    source = Path("control_plane/app/modules/agent/ports/repository.py").read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "Connection" not in source
    assert "Any" not in source
    assert "**values" not in source
    assert "AgentUnitOfWork" in source
    assert "AgentIdempotencyRecord" in source


def test_repository_round_trips_platform_dtos_without_runtime_leaks(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        _seed(repository)
        assert repository.definition_by_id(DEFINITION.id, DEFINITION.version) == DEFINITION
        assert repository.run_by_id(RUN.id) == RUN
        assert repository.attempt_by_id(ATTEMPT.id) == ATTEMPT
        binding = repository.binding_by_attempt_id(ATTEMPT.id)
        event = repository.append_event(EVENT)
        assert repository.insert_workflow_command(COMMAND) == COMMAND

    assert binding == BINDING
    assert binding is not None
    assert (binding.source, binding.digest) == (BINDING.source, BINDING.digest)
    assert event == EVENT
    with pytest.raises(TypeError):
        event.data["bindingSource"] = "changed"  # type: ignore[index]


def test_exact_canonical_event_replay_is_idempotent_and_changed_replay_conflicts(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        _seed(repository)
        assert repository.append_event(EVENT) == EVENT
        assert repository.append_event(EVENT) == EVENT
        with pytest.raises(EventReplayConflict):
            repository.append_event(EVENT.model_copy(update={"summary": "changed"}))
        with pytest.raises(EventReplayConflict):
            repository.append_event(
                EVENT.model_copy(update={"id": "10000000-0000-0000-0000-000000000820"})
            )
        assert repository.event_by_id(EVENT.id) == EVENT


def test_waiting_attempt_round_trips_its_immutable_checkpoint(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    waiting = ATTEMPT.model_copy(
        update={"state": AttemptState.WAITING_INPUT, "checkpoint": CHECKPOINT}
    )
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        assert repository.insert_definition(DEFINITION) == DEFINITION
        assert repository.insert_run(RUN) == RUN
        assert repository.insert_checkpoint(waiting.id, CHECKPOINT) == CHECKPOINT
        assert repository.insert_attempt(waiting) == waiting
        assert repository.insert_binding(waiting.id, BINDING) == BINDING
        loaded = repository.attempt_by_id(waiting.id)

    assert loaded == waiting
    assert loaded is not None
    assert loaded.checkpoint == CHECKPOINT


def test_concurrent_exact_replay_returns_prior_event_without_aborting_loser_transaction(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    first_inserted = Event()
    second_started = Event()
    release_first = Event()
    results: list[CanonicalEventInput] = []
    errors: list[BaseException] = []

    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        _seed(repository)

    def first_transaction() -> None:
        try:
            with isolated_agent_database.runtime.begin() as db:
                results.append(SqlAlchemyAgentRepository(db).append_event(EVENT))
                first_inserted.set()
                assert release_first.wait(timeout=5)
        except BaseException as error:  # pragma: no cover - asserted by the parent thread
            errors.append(error)

    def second_transaction() -> None:
        try:
            assert first_inserted.wait(timeout=5)
            with isolated_agent_database.runtime.begin() as db:
                second_started.set()
                results.append(SqlAlchemyAgentRepository(db).append_event(EVENT))
        except BaseException as error:  # pragma: no cover - asserted by the parent thread
            errors.append(error)

    first = Thread(target=first_transaction)
    second = Thread(target=second_transaction)
    first.start()
    second.start()
    assert second_started.wait(timeout=5)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert results == [EVENT, EVENT]


def test_concurrent_altered_replay_conflicts_without_overwriting_evidence(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    first_inserted = Event()
    second_started = Event()
    release_first = Event()
    errors: list[BaseException] = []

    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        _seed(repository)

    def first_transaction() -> None:
        with isolated_agent_database.runtime.begin() as db:
            SqlAlchemyAgentRepository(db).append_event(EVENT)
            first_inserted.set()
            assert release_first.wait(timeout=5)

    def second_transaction() -> None:
        assert first_inserted.wait(timeout=5)
        try:
            with isolated_agent_database.runtime.begin() as db:
                second_started.set()
                SqlAlchemyAgentRepository(db).append_event(
                    EVENT.model_copy(update={"summary": "changed concurrently"})
                )
        except BaseException as error:  # pragma: no cover - asserted by the parent thread
            errors.append(error)

    first = Thread(target=first_transaction)
    second = Thread(target=second_transaction)
    first.start()
    second.start()
    assert second_started.wait(timeout=5)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], EventReplayConflict)
    with isolated_agent_database.runtime.connect() as db:
        assert SqlAlchemyAgentRepository(db).event_by_id(EVENT.id) == EVENT


def test_repository_exposes_explicit_row_lock_and_revision_compare_and_set(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        _seed(repository)
        assert repository.attempt_by_id(ATTEMPT.id, for_update=True) == ATTEMPT
        updated = repository.compare_and_set_attempt(
            ATTEMPT.id,
            expected_revision=1,
            mutation=AttemptMutation(
                state=AttemptState.RUNNING,
                runner_generation=1,
                fencing_token="fence-1",
                checkpoint=None,
                event_sequence=0,
                waiting_deadline=None,
                terminal_evidence=None,
                now=NOW,
            ),
        )
        stale = repository.compare_and_set_attempt(
            ATTEMPT.id,
            expected_revision=1,
            mutation=AttemptMutation(
                state=AttemptState.FINALIZING,
                runner_generation=1,
                fencing_token="fence-1",
                checkpoint=None,
                event_sequence=0,
                waiting_deadline=None,
                terminal_evidence=None,
                now=NOW,
            ),
        )

    assert updated is not None
    assert updated.state is AttemptState.RUNNING
    assert updated.revision == 2
    assert stale is None


def test_competing_attempt_cas_allows_exactly_one_revision_winner(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    barrier = Barrier(3)
    results: list[AgentAttempt | None] = []
    errors: list[BaseException] = []
    with isolated_agent_database.runtime.begin() as db:
        _seed(SqlAlchemyAgentRepository(db))

    def compete(state: AttemptState) -> None:
        try:
            barrier.wait(timeout=5)
            with isolated_agent_database.runtime.begin() as db:
                results.append(
                    SqlAlchemyAgentRepository(db).compare_and_set_attempt(
                        ATTEMPT.id,
                        expected_revision=1,
                        mutation=AttemptMutation(
                            state=state,
                            runner_generation=1,
                            fencing_token="fence-1",
                            checkpoint=None,
                            event_sequence=0,
                            waiting_deadline=None,
                            terminal_evidence=None,
                            now=NOW,
                        ),
                    )
                )
        except BaseException as error:  # pragma: no cover - asserted by parent thread
            errors.append(error)

    first = Thread(target=compete, args=(AttemptState.RUNNING,))
    second = Thread(target=compete, args=(AttemptState.FINALIZING,))
    first.start()
    second.start()
    barrier.wait(timeout=5)
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert sum(result is not None for result in results) == 1
    with isolated_agent_database.runtime.connect() as db:
        current = SqlAlchemyAgentRepository(db).attempt_by_id(ATTEMPT.id)
    assert current is not None and current.revision == 2


def test_repository_exposes_full_mutation_dispatch_and_sealed_idempotency_primitives(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    idempotency = AgentIdempotencyRecord(
        id="10000000-0000-0000-0000-000000000819",
        actor="employee-815",
        operation="agent.run.start",
        key="idempotency-819",
        request_fingerprint="sha256:" + "b" * 64,
        state=IdempotencyState.IN_PROGRESS,
        http_status=None,
        result_metadata=None,
        sealed_response=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        _seed(repository)
        assert repository.insert_workflow_command(COMMAND) == COMMAND
        attempt = repository.compare_and_set_attempt(
            ATTEMPT.id,
            expected_revision=1,
            mutation=AttemptMutation(
                state=AttemptState.RUNNING,
                runner_generation=1,
                fencing_token="fence-1",
                checkpoint=None,
                event_sequence=1,
                waiting_deadline=None,
                terminal_evidence=None,
                now=NOW,
            ),
        )
        run = repository.compare_and_set_run(
            RUN.id,
            expected_revision=1,
            mutation=RunMutation(
                state=RunState.ACTIVE,
                latest_attempt_id=ATTEMPT.id,
                now=NOW,
            ),
        )
        reservation = repository.reserve_idempotency(idempotency)
        replay = repository.reserve_idempotency(idempotency)
        completion = IdempotencyCompletion(
            http_status=202,
            result_metadata={"runId": RUN.id},
            sealed_response=b"sealed-agent-response",
            now=NOW,
        )
        completed = repository.complete_idempotency(
            idempotency.id,
            completion=completion,
        )
        second_completion = repository.complete_idempotency(
            idempotency.id,
            completion=IdempotencyCompletion(
                http_status=200,
                result_metadata={"runId": "overwritten"},
                sealed_response=b"overwritten",
                now=NOW + timedelta(minutes=1),
            ),
        )
        sealed = repository.idempotency_by_scope(
            idempotency.actor,
            idempotency.operation,
            idempotency.key,
        )

    claim_token = "10000000-0000-0000-0000-000000000830"
    claim_lease_until = NOW + timedelta(seconds=30)
    with isolated_agent_database.runtime.begin() as db:
        claimed = SqlAlchemyAgentRepository(db).claim_workflow_commands(
            limit=1,
            now=NOW,
            claim_owner="repository-test-dispatcher",
            claim_token=claim_token,
            claim_lease_until=claim_lease_until,
            claim_mode=WorkflowClaimMode.DISPATCH,
        )
    with isolated_agent_database.runtime.begin() as db:
        dispatched = SqlAlchemyAgentRepository(db).record_workflow_command_dispatch(
            COMMAND.id,
            expected_state=WorkflowCommandState.PLANNED,
            expected_claim_token=claim_token,
            mutation=WorkflowDispatchMutation(
                state=WorkflowCommandState.DISPATCHED,
                receipt={"commandKey": COMMAND.command_key, "outcome": "ACCEPTED"},
                error_code=None,
                now=NOW,
            ),
        )

    assert attempt is not None and attempt.event_sequence == 1
    assert run is not None and run.revision == 2
    assert claimed == (
        COMMAND.model_copy(
            update={
                "dispatch_attempts": 1,
                "updated_at": NOW,
                "claim_owner": "repository-test-dispatcher",
                "claim_token": claim_token,
                "claim_lease_until": claim_lease_until,
                "claim_mode": WorkflowClaimMode.DISPATCH,
            }
        ),
    )
    assert dispatched is not None and dispatched.state is WorkflowCommandState.DISPATCHED
    assert reservation.created is True
    assert replay.created is False and replay.record == idempotency
    assert completed is not None and completed.sealed_response == b"sealed-agent-response"
    assert second_completion is None
    assert sealed == completed
    assert sealed is not None
    assert sealed.state is IdempotencyState.COMPLETED
    assert sealed.result_metadata == {"runId": RUN.id}
    assert sealed.sealed_response == b"sealed-agent-response"
    assert sealed.completed_at == NOW


@pytest.mark.parametrize(
    "provided",
    [
        (False, False, False, False),
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (False, False, False, True),
        (True, True, False, False),
        (True, False, True, False),
        (True, False, False, True),
        (False, True, True, False),
        (False, True, False, True),
        (False, False, True, True),
        (True, True, True, False),
        (True, True, False, True),
        (True, False, True, True),
        (False, True, True, True),
    ],
)
def test_workflow_claim_rejects_every_missing_or_partial_fence_tuple(
    isolated_agent_database: IsolatedAgentDatabase,
    provided: tuple[bool, bool, bool, bool],
) -> None:
    owner, token, lease, mode = provided
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        with pytest.raises(ValueError, match="owner, token, lease, and mode are required"):
            repository.claim_workflow_commands(
                limit=1,
                now=NOW,
                claim_owner="dispatcher" if owner else None,
                claim_token="10000000-0000-0000-0000-000000000831" if token else None,
                claim_lease_until=NOW + timedelta(seconds=30) if lease else None,
                claim_mode=WorkflowClaimMode.DISPATCH if mode else None,
            )


def test_workflow_completion_rejects_an_absent_claim_fence(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_workflow_command(COMMAND)
        with pytest.raises(ValueError, match="workflow completion claim token is required"):
            repository.record_workflow_command_dispatch(
                COMMAND.id,
                expected_state=WorkflowCommandState.PLANNED,
                expected_claim_token=None,
                mutation=WorkflowDispatchMutation(
                    state=WorkflowCommandState.DISPATCHED,
                    receipt={"commandKey": COMMAND.command_key, "outcome": "ACCEPTED"},
                    error_code=None,
                    now=NOW,
                ),
            )


def test_workflow_completion_rejects_stale_token_after_committed_lease_takeover(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    old_token = "10000000-0000-0000-0000-000000000832"
    current_token = "10000000-0000-0000-0000-000000000833"
    with isolated_agent_database.runtime.begin() as db:
        SqlAlchemyAgentRepository(db).insert_workflow_command(COMMAND)
    with isolated_agent_database.runtime.begin() as db:
        claimed = SqlAlchemyAgentRepository(db).claim_workflow_commands(
            limit=1,
            now=NOW,
            claim_owner="dispatcher-old",
            claim_token=old_token,
            claim_lease_until=NOW + timedelta(seconds=30),
            claim_mode=WorkflowClaimMode.DISPATCH,
        )
    assert len(claimed) == 1
    with isolated_agent_database.runtime.begin() as db:
        reclaimed = SqlAlchemyAgentRepository(db).claim_workflow_commands(
            limit=1,
            now=NOW + timedelta(seconds=31),
            claim_owner="reconciler-current",
            claim_token=current_token,
            claim_lease_until=NOW + timedelta(seconds=61),
            claim_mode=WorkflowClaimMode.RECONCILE,
        )
    assert len(reclaimed) == 1
    with isolated_agent_database.runtime.begin() as db:
        stale = SqlAlchemyAgentRepository(db).record_workflow_command_dispatch(
            COMMAND.id,
            expected_state=WorkflowCommandState.PLANNED,
            expected_claim_token=old_token,
            mutation=WorkflowDispatchMutation(
                state=WorkflowCommandState.DISPATCHED,
                receipt={"commandKey": COMMAND.command_key, "outcome": "ACCEPTED"},
                error_code=None,
                now=NOW + timedelta(seconds=31),
            ),
        )
    assert stale is None
    with isolated_agent_database.runtime.begin() as db:
        completed = SqlAlchemyAgentRepository(db).record_workflow_command_dispatch(
            COMMAND.id,
            expected_state=WorkflowCommandState.PLANNED,
            expected_claim_token=current_token,
            mutation=WorkflowDispatchMutation(
                state=WorkflowCommandState.DISPATCHED,
                receipt={"commandKey": COMMAND.command_key, "outcome": "ACCEPTED"},
                error_code=None,
                now=NOW + timedelta(seconds=31),
            ),
        )
    assert completed is not None
    assert completed.state is WorkflowCommandState.DISPATCHED
    assert completed.claim_token is None


def test_platform_transaction_runner_persists_agent_and_audit_together_or_rolls_back(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    runner = SqlAlchemyAgentTransactionRunner(isolated_agent_database.runtime)
    audit = AgentAuditAppend(
        id="agent-task2-audit-821",
        occurred_at=NOW,
        actor="employee-815",
        actor_type="USER",
        action="agent.persistence.test",
        target_type="agent_run",
        target_id=RUN.id,
        result="SUCCESS",
        reason="TASK_2_TRANSACTION_PLUMBING",
        correlation_id="correlation-821",
        schema_version=1,
    )

    def success(unit_of_work: AgentUnitOfWork) -> None:
        unit_of_work.repository().insert_definition(DEFINITION)
        unit_of_work.repository().insert_run(RUN)
        unit_of_work.append_audit_event(audit)

    runner(success)
    with isolated_agent_database.owner.connect() as db:
        repository = SqlAlchemyAgentRepository(db)
        assert repository.run_by_id(RUN.id) == RUN
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE id=:id"), {"id": audit.id}
            ).scalar_one()
            == 1
        )

    rolled_definition = DEFINITION.model_copy(
        update={
            "id": "10000000-0000-0000-0000-000000000822",
            "name": "agent/test-rollback",
        }
    )
    rolled_run = RUN.model_copy(
        update={
            "id": "10000000-0000-0000-0000-000000000823",
            "definition_id": rolled_definition.id,
            "latest_attempt_id": "10000000-0000-0000-0000-000000000824",
        }
    )
    rolled_audit = audit.model_copy(
        update={
            "id": "agent-task2-audit-825",
            "target_id": rolled_run.id,
            "correlation_id": "correlation-825",
        }
    )

    def fail(unit_of_work: AgentUnitOfWork) -> None:
        unit_of_work.repository().insert_definition(rolled_definition)
        unit_of_work.repository().insert_run(rolled_run)
        unit_of_work.append_audit_event(rolled_audit)
        raise RuntimeError("prove transactional rollback")

    with pytest.raises(RuntimeError, match="prove transactional rollback"):
        runner(fail)

    with isolated_agent_database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM agent.agent_run WHERE id=CAST(:id AS UUID)"),
                {"id": rolled_run.id},
            ).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE id=:id"),
                {"id": rolled_audit.id},
            ).scalar_one()
            == 0
        )


def test_runtime_role_cannot_update_or_delete_immutable_binding_or_event(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        _seed(repository)
        repository.append_event(EVENT)

    with pytest.raises(DBAPIError):
        with isolated_agent_database.runtime.begin() as db:
            db.execute(
                text("UPDATE agent.execution_binding SET digest='0' WHERE id=:id"),
                {"id": BINDING.id},
            )
    with pytest.raises(DBAPIError):
        with isolated_agent_database.runtime.begin() as db:
            db.execute(
                text("DELETE FROM agent.canonical_event WHERE event_id=:id"),
                {"id": EVENT.id},
            )
