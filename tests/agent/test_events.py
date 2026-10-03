from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock, Thread
from uuid import UUID

import pytest
from sqlalchemy import text

from control_plane.app.modules.agent import accept_workflow_event, get_run, list_events, start_run
from control_plane.app.modules.agent.adapters.dev_policy import (
    DevActorResolver,
    DevEventCursorCodec,
    DevExecutionBindingPolicy,
)
from control_plane.app.modules.agent.adapters.sqlalchemy import (
    SqlAlchemyAgentRepository,
    SqlAlchemyAgentTransactionRunner,
    SqlAlchemyAgentUnitOfWork,
)
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.events import (
    EventSequenceGap,
    StaleRunnerGeneration,
)
from control_plane.app.modules.agent.application.runs import StartRunCommand, StartRunResult
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentAuditAppend,
    AttemptState,
    CanonicalEventInput,
    EventReplayConflict,
)
from control_plane.app.modules.agent.ports import AgentUnitOfWork
from control_plane.app.modules.agent.ports.runtime import (
    RequirementExecutionContext,
    RequirementExecutionRequest,
)
from tests.agent.conftest import IsolatedAgentDatabase, TestSecretManager

NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
WORKSPACE_ID = "10000000-0000-0000-0000-000000000901"
REQUIREMENT_ID = "10000000-0000-0000-0000-000000000902"
WORK_ITEM_ID = "10000000-0000-0000-0000-000000000903"
DEFINITION_ID = "00000000-0000-0000-0000-000000000800"
BASE64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


class DeterministicIds:
    def __init__(self) -> None:
        self._next = 1200

    def __call__(self) -> str:
        value = str(UUID(int=self._next))
        self._next += 1
        return value


@dataclass(frozen=True, slots=True)
class StaticRequirementContext:
    def protect(
        self, command: RequirementExecutionRequest
    ) -> nullcontext[RequirementExecutionContext]:
        return nullcontext(self.resolve(command))

    def resolve(self, command: RequirementExecutionRequest) -> RequirementExecutionContext:
        return RequirementExecutionContext(
            workspace_id=command.workspace_id,
            requirement_id=command.requirement_id,
            work_item_id=command.work_item_id,
            assignment_id="10000000-0000-0000-0000-000000000904",
            goal_ref=f"requirement:{command.requirement_id}:work-item:{command.work_item_id}",
        )


class AlwaysActiveDefinitionAvailability:
    def is_active(self, _definition: object) -> bool:
        return True


def dependencies(database: IsolatedAgentDatabase, *, now: datetime = NOW) -> AgentDependencies:
    return AgentDependencies(
        transaction_runner=SqlAlchemyAgentTransactionRunner(database.runtime),
        requirement_context=StaticRequirementContext(),
        binding_policy=DevExecutionBindingPolicy(),
        definition_availability=AlwaysActiveDefinitionAvailability(),
        actor_resolver=DevActorResolver(),
        clock=lambda: now,
        new_id=DeterministicIds(),
        cursor_codec=DevEventCursorCodec(),
        secret_manager=TestSecretManager(),
    )


def start(deps: AgentDependencies, *, idempotency_key: str = "event-start-901") -> StartRunResult:
    return start_run(
        None,
        command=StartRunCommand(
            workspace_id=WORKSPACE_ID,
            requirement_id=REQUIREMENT_ID,
            work_item_id=WORK_ITEM_ID,
            definition_id=DEFINITION_ID,
            definition_version=1,
            goal="Create governed evidence",
            actor="employee-901",
            idempotency_key=idempotency_key,
            correlation_id="event-correlation-901",
        ),
        dependencies=deps,
    )


def event(
    attempt_id: str,
    *,
    event_id: str,
    event_type: str,
    sequence: int,
    generation: int = 1,
    data: dict[str, object] | None = None,
) -> CanonicalEventInput:
    return CanonicalEventInput(
        id=event_id,
        event_type=event_type,
        attempt_id=attempt_id,
        generation=generation,
        sequence=sequence,
        correlation_id="event-correlation-901",
        causation_id=None,
        trace_id="trace-901",
        span_id=f"span-{sequence}",
        summary=f"Platform event {event_type}",
        data=data or {},
    )


def advance_to_running(deps: AgentDependencies, attempt_id: str) -> None:
    accept_workflow_event(
        None,
        event=event(
            attempt_id,
            event_id="10000000-0000-0000-0000-000000001301",
            event_type="ATTEMPT_PROVISIONING",
            sequence=2,
        ),
        dependencies=deps,
    )
    accept_workflow_event(
        None,
        event=event(
            attempt_id,
            event_id="10000000-0000-0000-0000-000000001302",
            event_type="ATTEMPT_RUNNING",
            sequence=3,
        ),
        dependencies=deps,
    )


def waiting_event(
    attempt_id: str, *, generation: int = 1, sequence: int = 4
) -> CanonicalEventInput:
    return event(
        attempt_id,
        event_id="10000000-0000-0000-0000-000000001303",
        event_type="WAITING_INPUT",
        generation=generation,
        sequence=sequence,
        data={
            "checkpoint": {
                "id": "10000000-0000-0000-0000-000000001304",
                "artifact_id": "artifact-901",
                "artifact_version": "1",
                "content_sha256": "sha256:" + "a" * 64,
                "schema_version": "1",
                "adapter_version": "1",
                "classification": "INTERNAL",
            },
            "waitingDeadline": (NOW + timedelta(hours=1)).isoformat(),
            "question": {"prompt": "请确认受控测试的输入。"},
        },
    )


def attempt_state(database: IsolatedAgentDatabase, attempt_id: str) -> tuple[str, int, int, int]:
    with database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT state, runner_generation, event_sequence, revision "
                "FROM agent.agent_attempt WHERE id=CAST(:id AS UUID)"
            ),
            {"id": attempt_id},
        ).one()
    return (row.state, row.runner_generation, row.event_sequence, row.revision)


def decode_cursor_segment(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def encode_cursor_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def noncanonical_pad_bit_variant(segment: str) -> str:
    remainder = len(segment) % 4
    unused_bits = {2: 4, 3: 2}[remainder]
    final_index = BASE64URL_ALPHABET.index(segment[-1])
    assert final_index & ((1 << unused_bits) - 1) == 0
    variant = segment[:-1] + BASE64URL_ALPHABET[final_index | 1]
    assert variant != segment
    assert decode_cursor_segment(variant) == decode_cursor_segment(segment)
    return variant


@dataclass
class BarrierState:
    lock: Lock = field(default_factory=Lock)
    waited: int = 0


class BarrierRepository(SqlAlchemyAgentRepository):
    def __init__(
        self,
        delegate: SqlAlchemyAgentRepository,
        barrier: Barrier,
        state: BarrierState,
        *,
        lock_attempt: bool = False,
    ) -> None:
        super().__init__(delegate.db)
        self._barrier = barrier
        self._lock_attempt = lock_attempt
        self._state = state

    def event_by_id(self, event_id: str) -> CanonicalEventInput | None:
        with self._state.lock:
            should_wait = self._state.waited < 2
            self._state.waited += 1
        if should_wait:
            self._barrier.wait(timeout=5)
        return super().event_by_id(event_id)

    def attempt_by_id(self, attempt_id: str, *, for_update: bool = False) -> AgentAttempt | None:
        if self._lock_attempt and for_update:
            with self._state.lock:
                should_wait = self._state.waited < 1
                self._state.waited += 1
            if should_wait:
                self._barrier.wait(timeout=5)
        return super().attempt_by_id(attempt_id, for_update=for_update)


class BarrierUnitOfWork:
    def __init__(self, delegate: SqlAlchemyAgentUnitOfWork, repository: BarrierRepository) -> None:
        self._delegate = delegate
        self._repository = repository

    def repository(self) -> BarrierRepository:
        return self._repository

    def append_audit_event(self, event: AgentAuditAppend) -> None:
        self._delegate.append_audit_event(event)


class BarrierTransactionRunner:
    def __init__(
        self, database: IsolatedAgentDatabase, barrier: Barrier, *, lock_attempt: bool
    ) -> None:
        self._delegate = SqlAlchemyAgentTransactionRunner(database.runtime)
        self._barrier = barrier
        self._lock_attempt = lock_attempt
        self._state = BarrierState()

    def __call__[T](self, operation: Callable[[AgentUnitOfWork], T]) -> T:
        def wrapped(uow: SqlAlchemyAgentUnitOfWork) -> T:
            repository = BarrierRepository(
                uow.repository(), self._barrier, self._state, lock_attempt=self._lock_attempt
            )
            return operation(BarrierUnitOfWork(uow, repository))

        return self._delegate(wrapped)


def test_waiting_event_atomically_persists_checkpoint(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)

    accepted = accept_workflow_event(
        None, event=waiting_event(started.attempt.id), dependencies=deps
    )

    assert accepted.attempt.state is AttemptState.WAITING_INPUT
    assert accepted.checkpoint is not None
    assert accepted.checkpoint.content_sha256 == "sha256:" + "a" * 64
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.checkpoint")).scalar_one() == 1


def test_waiting_event_without_checkpoint_leaves_running_attempt_unchanged(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)
    invalid = waiting_event(started.attempt.id).model_dump()
    missing_checkpoint = CanonicalEventInput.model_construct(**invalid)
    object.__setattr__(
        missing_checkpoint, "data", {"waitingDeadline": (NOW + timedelta(hours=1)).isoformat()}
    )

    with pytest.raises(ValueError, match="checkpoint"):
        accept_workflow_event(None, event=missing_checkpoint, dependencies=deps)

    assert attempt_state(isolated_agent_database, started.attempt.id) == ("RUNNING", 1, 3, 5)
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.checkpoint")).scalar_one() == 0


def test_checkpoint_conflict_rolls_back_waiting_event_and_attempt(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)
    conflict = waiting_event(started.attempt.id)
    checkpoint = conflict.data["checkpoint"]
    assert isinstance(checkpoint, Mapping)
    with isolated_agent_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO agent.checkpoint (id, attempt_id, artifact_id, artifact_version, "
                "content_sha256, schema_version, adapter_version, classification) VALUES "
                "(CAST(:id AS UUID), CAST(:attempt_id AS UUID), "
                "'existing', '1', :hash, '1', '1', 'INTERNAL')"
            ),
            {
                "id": checkpoint["id"],
                "attempt_id": started.attempt.id,
                "hash": "sha256:" + "b" * 64,
            },
        )
    before = attempt_state(isolated_agent_database, started.attempt.id)
    with pytest.raises(Exception, match="duplicate|unique"):
        accept_workflow_event(None, event=conflict, dependencies=deps)
    assert attempt_state(isolated_agent_database, started.attempt.id) == before
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.canonical_event")).scalar_one() == 3
        assert db.execute(text("SELECT count(*) FROM agent.checkpoint")).scalar_one() == 1
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE action='agent.event.accept'")
            ).scalar_one()
            == 2
        )


def test_exact_replay_is_idempotent_but_altered_replay_preserves_evidence(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    provisioning = event(
        started.attempt.id,
        event_id="10000000-0000-0000-0000-000000001305",
        event_type="ATTEMPT_PROVISIONING",
        sequence=2,
    )

    first = accept_workflow_event(None, event=provisioning, dependencies=deps)
    replay = accept_workflow_event(None, event=provisioning, dependencies=deps)

    assert replay == first
    with pytest.raises(EventReplayConflict):
        accept_workflow_event(
            None,
            event=provisioning.model_copy(update={"summary": "changed"}),
            dependencies=deps,
        )
    assert attempt_state(isolated_agent_database, started.attempt.id) == ("PROVISIONING", 1, 2, 4)


@pytest.mark.parametrize("altered", [False, True])
def test_concurrent_first_delivery_replay_rechecks_persisted_evidence_after_locks(
    isolated_agent_database: IsolatedAgentDatabase, altered: bool
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    incoming = event(
        started.attempt.id,
        event_id="10000000-0000-0000-0000-000000001350",
        event_type="ATTEMPT_PROVISIONING",
        sequence=2,
    )
    barrier = Barrier(2)
    deps = replace(
        deps,
        transaction_runner=BarrierTransactionRunner(
            isolated_agent_database, barrier, lock_attempt=False
        ),
    )
    results: list[object] = []
    errors: list[BaseException] = []

    def deliver(candidate: CanonicalEventInput) -> None:
        try:
            results.append(accept_workflow_event(None, event=candidate, dependencies=deps))
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    first = Thread(target=deliver, args=(incoming,))
    second = Thread(
        target=deliver,
        args=(incoming.model_copy(update={"summary": "changed"}) if altered else incoming,),
    )
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(results) == (1 if altered else 2)
    assert len(errors) == (1 if altered else 0)
    if altered:
        assert isinstance(errors[0], EventReplayConflict)
    assert attempt_state(isolated_agent_database, started.attempt.id) == ("PROVISIONING", 1, 2, 4)
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.canonical_event")).scalar_one() == 2


def test_stale_generation_and_sequence_gap_do_not_mutate_attempt(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    stale = event(
        started.attempt.id,
        event_id="10000000-0000-0000-0000-000000001306",
        event_type="ATTEMPT_PROVISIONING",
        generation=2,
        sequence=1,
    )
    gap = event(
        started.attempt.id,
        event_id="10000000-0000-0000-0000-000000001307",
        event_type="ATTEMPT_PROVISIONING",
        sequence=3,
    )

    with pytest.raises(StaleRunnerGeneration):
        accept_workflow_event(None, event=stale, dependencies=deps)
    with pytest.raises(EventSequenceGap):
        accept_workflow_event(None, event=gap, dependencies=deps)

    assert attempt_state(isolated_agent_database, started.attempt.id) == ("QUEUED", 1, 1, 3)


def test_queries_return_platform_views_and_opaque_event_cursors(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)

    view = get_run(None, run_id=started.run.id, dependencies=deps)
    first = list_events(None, run_id=started.run.id, cursor=None, limit=2, dependencies=deps)
    second = list_events(
        None, run_id=started.run.id, cursor=first.next_cursor, limit=2, dependencies=deps
    )

    assert view.run.id == started.run.id
    assert view.attempts[0].id == started.attempt.id
    assert len(first.items) == 2
    assert first.next_cursor is not None
    assert "10000000" not in first.next_cursor
    assert [item.sequence for item in second.items] == [3]


def issued_event_cursor(
    database: IsolatedAgentDatabase,
) -> tuple[AgentDependencies, StartRunResult, str, str]:
    deps = dependencies(database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)
    first_page = list_events(None, run_id=started.run.id, cursor=None, limit=1, dependencies=deps)
    assert first_page.next_cursor is not None
    return deps, started, first_page.next_cursor, first_page.items[-1].id


def test_event_cursor_rejects_noncanonical_pad_bits_that_decode_to_issued_signature(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started, issued, _event_id = issued_event_cursor(isolated_agent_database)
    prefix, payload, signature = issued.split(".")
    tampered = ".".join((prefix, payload, noncanonical_pad_bit_variant(signature)))

    with pytest.raises(ValueError, match="cursor"):
        list_events(
            None,
            run_id=started.run.id,
            cursor=tampered,
            limit=1,
            dependencies=deps,
        )


def test_event_cursor_rejects_payload_signature_and_valid_shape_hmac_tampering(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started, issued, _event_id = issued_event_cursor(isolated_agent_database)
    prefix, payload, signature = issued.split(".")
    changed_payload = bytearray(decode_cursor_segment(payload))
    changed_payload[0] ^= 1
    changed_signature = bytearray(decode_cursor_segment(signature))
    changed_signature[0] ^= 1
    tampered = (
        ".".join((prefix, encode_cursor_segment(bytes(changed_payload)), signature)),
        ".".join((prefix, payload, encode_cursor_segment(bytes(changed_signature)))),
        ".".join((prefix, payload, encode_cursor_segment(bytes(32)))),
    )

    for cursor in tampered:
        with pytest.raises(ValueError, match="cursor"):
            list_events(
                None,
                run_id=started.run.id,
                cursor=cursor,
                limit=1,
                dependencies=deps,
            )


def test_event_cursor_rejects_unsupported_envelope_and_signed_payload_versions(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps, started, issued, event_id = issued_event_cursor(isolated_agent_database)
    _prefix, payload, signature = issued.split(".")

    for prefix in ("x1", "v0", "v2"):
        with pytest.raises(ValueError, match="cursor"):
            list_events(
                None,
                run_id=started.run.id,
                cursor=".".join((prefix, payload, signature)),
                limit=1,
                dependencies=deps,
            )

    class VersionTwoPayloadCodec(DevEventCursorCodec):
        _VERSION = 2

    version_two = VersionTwoPayloadCodec().encode(run_id=started.run.id, event_id=event_id)
    _version_two_prefix, version_two_payload, version_two_signature = version_two.split(".")
    signed_unsupported_payload = ".".join(("v1", version_two_payload, version_two_signature))
    with pytest.raises(ValueError, match="cursor"):
        list_events(
            None,
            run_id=started.run.id,
            cursor=signed_unsupported_payload,
            limit=1,
            dependencies=deps,
        )


@pytest.mark.parametrize("cursor", ["%%%", "eyJldmVudCI6ImZvcmdlZCJ9", "v1.not-a-signature"])
def test_event_cursor_rejects_invalid_or_forged_values(
    isolated_agent_database: IsolatedAgentDatabase, cursor: str
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)

    with pytest.raises(ValueError, match="cursor"):
        list_events(None, run_id=started.run.id, cursor=cursor, limit=2, dependencies=deps)


def test_event_cursor_is_bound_to_its_run(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    first_run = start(deps)
    advance_to_running(deps, first_run.attempt.id)
    second_run = start(deps, idempotency_key="event-start-902")
    first_page = list_events(None, run_id=first_run.run.id, cursor=None, limit=1, dependencies=deps)

    with pytest.raises(ValueError, match="cursor"):
        list_events(
            None,
            run_id=second_run.run.id,
            cursor=first_page.next_cursor,
            limit=1,
            dependencies=deps,
        )


def test_generation_two_begins_at_sequence_one_and_stale_rejection_preserves_row(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    from control_plane.app.modules.agent import resume_attempt
    from tests.agent.test_control import prepare_waiting, resume_command

    deps, started = prepare_waiting(isolated_agent_database)
    resumed = resume_attempt(None, command=resume_command(started, revision=6), dependencies=deps)
    accepted = accept_workflow_event(
        None,
        event=event(
            started.attempt.id,
            event_id="10000000-0000-0000-0000-000000001360",
            event_type="ATTEMPT_PROVISIONING",
            generation=2,
            sequence=1,
        ),
        dependencies=deps,
    )
    before = attempt_state(isolated_agent_database, started.attempt.id)
    with pytest.raises(StaleRunnerGeneration):
        accept_workflow_event(
            None,
            event=event(
                started.attempt.id,
                event_id="10000000-0000-0000-0000-000000001361",
                event_type="ATTEMPT_RUNNING",
                generation=1,
                sequence=5,
            ),
            dependencies=deps,
        )
    assert accepted.event.sequence == 1
    assert (
        resumed.attempt.fencing_token
        and resumed.attempt.fencing_token != started.attempt.fencing_token
    )
    assert attempt_state(isolated_agent_database, started.attempt.id) == before
