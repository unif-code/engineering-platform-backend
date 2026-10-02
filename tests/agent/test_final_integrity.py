"""Regression evidence for immutable receipts and authenticated command replay."""

from dataclasses import replace
from importlib import import_module
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from alembic import command as migration
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from control_plane.app.modules.agent import (
    AgentReplayUnavailable,
    EventReplayUnavailable,
    accept_workflow_event,
    cancel_attempt,
    resume_attempt,
)
from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
from control_plane.app.modules.agent.application.control import AttemptControlResult
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.events import (
    EventAcceptance,
)
from control_plane.app.modules.agent.application.runs import StartRunResult
from control_plane.app.modules.agent.domain import (
    AgentDefinition,
    CanonicalEventInput,
    CheckpointInput,
)
from control_plane.app.shared.idempotency import SealedIdempotentEnvelope
from control_plane.app.shared.security import (
    SecretMaterial,
    SecretMaterialUnavailable,
    seal,
    unseal,
)
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_control import cancel_command, resume_command
from tests.agent.test_events import (
    NOW,
    advance_to_running,
    dependencies,
    event,
    start,
    waiting_event,
)
from tests.agent.test_repository import ATTEMPT, BINDING, DEFINITION, EVENT, insert_predecessor_run
from tests.agent.test_start_run import AuditFailingTransactionRunner


def facts(database: IsolatedAgentDatabase) -> dict[str, list[dict[str, object]]]:
    """All protected durable facts, including the new append-only receipt."""
    tables = (
        "agent_definition",
        "agent_run",
        "agent_attempt",
        "execution_binding",
        "canonical_event",
        "event_acceptance_receipt",
        "checkpoint",
        "workflow_command",
        "idempotency_key",
    )
    with database.owner.connect() as db:
        result = {
            table: [
                dict(row)
                for row in db.execute(text(f"SELECT * FROM agent.{table} ORDER BY 1")).mappings()
            ]
            for table in tables
        }
        result["audit"] = [
            dict(row)
            for row in db.execute(text("SELECT * FROM audit.audit_event ORDER BY id")).mappings()
        ]
    return result


def assert_replay_unchanged(
    database: IsolatedAgentDatabase,
    deps: AgentDependencies,
    original: CanonicalEventInput,
    first: EventAcceptance,
) -> None:
    before = facts(database)
    assert accept_workflow_event(None, event=original, dependencies=deps) == first
    assert facts(database) == before


def test_original_event_acceptance_survives_resume_and_second_checkpoint(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)
    original = waiting_event(started.attempt.id)
    first = accept_workflow_event(None, event=original, dependencies=deps)
    assert_replay_unchanged(isolated_agent_database, deps, original, first)
    resume_attempt(None, command=resume_command(started, revision=6), dependencies=deps)
    assert_replay_unchanged(isolated_agent_database, deps, original, first)
    for sequence, kind in enumerate(("ATTEMPT_PROVISIONING", "ATTEMPT_RUNNING"), 1):
        accept_workflow_event(
            None,
            event=event(
                started.attempt.id,
                event_id=str(uuid4()),
                event_type=kind,
                generation=2,
                sequence=sequence,
            ),
            dependencies=deps,
        )
    data = original.model_dump(mode="json")["data"]
    data["checkpoint"]["id"] = str(uuid4())
    data["checkpoint"]["artifact_version"] = "2"
    second = original.model_copy(
        update={"id": str(uuid4()), "generation": 2, "sequence": 3, "data": data}
    )
    second_acceptance = accept_workflow_event(None, event=second, dependencies=deps)
    assert_replay_unchanged(isolated_agent_database, deps, original, first)
    canceled = cancel_attempt(
        None,
        command=cancel_command(started, revision=second_acceptance.attempt.revision),
        dependencies=deps,
    )
    accept_workflow_event(
        None,
        event=event(
            started.attempt.id,
            event_id=str(uuid4()),
            event_type="ATTEMPT_CANCELED",
            generation=2,
            sequence=4,
        ),
        dependencies=deps,
    )
    assert canceled.attempt.state == "CANCELING"
    assert_replay_unchanged(isolated_agent_database, deps, original, first)
    assert_replay_unchanged(isolated_agent_database, deps, second, second_acceptance)
    queued = EventAcceptance(event=started.event, attempt=started.attempt, checkpoint=None)
    assert_replay_unchanged(isolated_agent_database, deps, started.event, queued)


@pytest.mark.parametrize("kind", ["event", "checkpoint", "definition"])
def test_review_counterexamples_are_rejected_at_normal_ingress(kind: str) -> None:
    with pytest.raises(ValidationError):
        if kind == "event":
            payload = event(
                str(uuid4()), event_id=str(uuid4()), event_type="ATTEMPT_RUNNING", sequence=1
            ).model_dump()
            payload["correlation_id"] = "x" * 2049
            CanonicalEventInput.model_validate(payload)
        elif kind == "checkpoint":
            payload = waiting_event(str(uuid4())).model_dump(mode="json")["data"]["checkpoint"]
            payload["artifact_version"] = "x" * 201
            CheckpointInput.model_validate(payload)
        else:
            AgentDefinition(
                id=str(uuid4()),
                version=1,
                name="x" * 201,
                capability_declarations=(),
                skill_declarations=(),
                runtime_permissions=(),
                input_schema={},
                created_at=NOW,
            )


def test_start_response_is_not_persisted_as_plaintext(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    result = start(deps)
    with isolated_agent_database.owner.connect() as db:
        sealed = bytes(
            db.execute(text("SELECT sealed_response FROM agent.idempotency_key")).scalar_one()
        )
    assert result.attempt.fencing_token.encode() not in sealed
    assert result.run.id.encode() not in sealed
    plaintext = unseal(sealed, deps.secret_manager.load().idempotency_sealing_key)
    assert sealed != plaintext
    assert SealedIdempotentEnvelope.model_validate_json(
        plaintext
    ).response.body == result.model_dump(mode="json")


def test_receipts_are_append_only_and_downgrade_preserves_durable_evidence(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    start(dependencies(isolated_agent_database))
    before = facts(isolated_agent_database)
    for sql in (
        "UPDATE agent.event_acceptance_receipt SET schema_version=1",
        "DELETE FROM agent.event_acceptance_receipt",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            with isolated_agent_database.runtime.begin() as db:
                db.execute(text(sql))
    receipt_migration = import_module("migrations.agent.0003_event_acceptance_receipt")
    with pytest.raises(DBAPIError, match="durable receipts"):
        with isolated_agent_database.owner.begin() as db:
            with patch.object(receipt_migration, "op", Operations(MigrationContext.configure(db))):
                receipt_migration.downgrade()
    assert facts(isolated_agent_database) == before


def test_upgrade_keeps_predecessor_events_but_never_fabricates_acceptance(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    migration.downgrade(Config("alembic.ini"), "agent@0002_workflow_claim_lease")
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        insert_predecessor_run(db)
        repository.insert_attempt(ATTEMPT)
        repository.insert_binding(ATTEMPT.id, BINDING)
        repository.append_event(EVENT)
    with isolated_agent_database.owner.connect() as db:
        old_event = dict(db.execute(text("SELECT * FROM agent.canonical_event")).mappings().one())
    migration.upgrade(Config("alembic.ini"), "heads")
    before = facts(isolated_agent_database)
    assert before["canonical_event"] == [old_event]
    assert before["event_acceptance_receipt"] == []
    with pytest.raises(EventReplayUnavailable, match="unavailable"):
        accept_workflow_event(None, event=EVENT, dependencies=dependencies(isolated_agent_database))
    assert facts(isolated_agent_database) == before


@pytest.mark.parametrize("historical_absence", [False, True])
def test_start_replay_preserves_original_source_and_sealed_predecessor_receipts(
    isolated_agent_database: IsolatedAgentDatabase,
    historical_absence: bool,
) -> None:
    deps = dependencies(isolated_agent_database)
    created = start(deps)
    assert created.run.business_context is not None
    if historical_absence:
        material = deps.secret_manager.load()
        with isolated_agent_database.owner.begin() as db:
            sealed = db.execute(
                text("SELECT sealed_response FROM agent.idempotency_key")
            ).scalar_one()
            envelope = SealedIdempotentEnvelope.model_validate_json(
                unseal(sealed, material.idempotency_sealing_key)
            )
            body = envelope.response.model_dump()["body"]
            del body["run"]["business_context"]
            predecessor = envelope.model_copy(
                update={"response": envelope.response.model_copy(update={"body": body})}
            )
            db.execute(
                text("UPDATE agent.idempotency_key SET sealed_response=:sealed"),
                {
                    "sealed": seal(
                        predecessor.model_dump_json().encode(), material.idempotency_sealing_key
                    )
                },
            )
            db.execute(
                text(
                    "UPDATE agent.agent_run SET requirement_id=NULL,work_item_id=NULL,"
                    "assignment_id=NULL"
                )
            )
    current_owner = Mock()
    current_owner.resolve.side_effect = AssertionError(
        "replay cannot re-resolve a reassigned owner"
    )
    before = facts(isolated_agent_database)
    replay = start(replace(deps, requirement_context=current_owner))
    assert replay.run.business_context == (
        None if historical_absence else created.run.business_context
    )
    current_owner.resolve.assert_not_called()
    assert facts(isolated_agent_database) == before


@pytest.mark.parametrize("during_start", [False, True])
def test_audit_failure_rolls_back_receipt_event_and_transition(
    isolated_agent_database: IsolatedAgentDatabase,
    during_start: bool,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = None if during_start else start(deps)
    before = facts(isolated_agent_database)
    failing = replace(
        deps, transaction_runner=AuditFailingTransactionRunner(isolated_agent_database)
    )
    with pytest.raises(RuntimeError, match="audit append unavailable"):
        if started is None:
            start(failing)
        else:
            accept_workflow_event(
                None,
                event=event(
                    started.attempt.id,
                    event_id=str(uuid4()),
                    event_type="ATTEMPT_PROVISIONING",
                    sequence=2,
                ),
                dependencies=failing,
            )
    assert facts(isolated_agent_database) == before


@pytest.mark.parametrize("operation", ["start", "cancel", "resume"])
@pytest.mark.parametrize(
    "attack", ["tamper", "actor", "operation", "key", "fingerprint", "status", "plaintext"]
)
def test_authenticated_replay_fails_closed_without_new_protected_facts(
    isolated_agent_database: IsolatedAgentDatabase,
    operation: str,
    attack: str,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    if operation == "start":

        def replay() -> StartRunResult | AttemptControlResult:
            return start(deps)

        result: StartRunResult | AttemptControlResult = started
        key = "event-start-901"
    elif operation == "cancel":
        cancel = cancel_command(started, revision=3)

        def replay() -> StartRunResult | AttemptControlResult:
            return cancel_attempt(None, command=cancel, dependencies=deps)

        result = replay()
        key = cancel.idempotency_key
    else:
        advance_to_running(deps, started.attempt.id)
        accept_workflow_event(None, event=waiting_event(started.attempt.id), dependencies=deps)
        resume = resume_command(started, revision=6)

        def replay() -> StartRunResult | AttemptControlResult:
            return resume_attempt(None, command=resume, dependencies=deps)

        result = replay()
        key = resume.idempotency_key
    with isolated_agent_database.owner.begin() as db:
        row = (
            db.execute(text("SELECT * FROM agent.idempotency_key WHERE key=:key"), {"key": key})
            .mappings()
            .one()
        )
        encrypted = bytes(row["sealed_response"])
        assert result.attempt.fencing_token.encode() not in encrypted
        material = deps.secret_manager.load().idempotency_sealing_key
        plaintext = unseal(encrypted, material)
        assert encrypted != plaintext
        assert SealedIdempotentEnvelope.model_validate_json(
            plaintext
        ).response.body == result.model_dump(mode="json")
        if attack == "tamper":
            changed = bytes([encrypted[0] ^ 1]) + encrypted[1:]
        elif attack == "plaintext":
            changed = result.model_dump_json().encode()
            db.execute(
                text("UPDATE agent.idempotency_key SET result_metadata='{}'::jsonb WHERE key=:key"),
                {"key": key},
            )
        else:
            envelope = SealedIdempotentEnvelope.model_validate_json(
                unseal(encrypted, material)
            ).model_dump(mode="json")
            if attack == "status":
                envelope["response"]["status_code"] = 201
            else:
                field = {"key": "idempotency_key", "fingerprint": "request_fingerprint"}.get(
                    attack, attack
                )
                envelope[field] = "changed-scope"
            changed = seal(
                SealedIdempotentEnvelope.model_validate(envelope).model_dump_json().encode(),
                material,
            )
        db.execute(
            text("UPDATE agent.idempotency_key SET sealed_response=:sealed WHERE key=:key"),
            {"key": key, "sealed": changed},
        )
    before = facts(isolated_agent_database)
    with pytest.raises(AgentReplayUnavailable, match="unavailable"):
        replay()
    assert facts(isolated_agent_database) == before


def test_swapping_complete_ciphertexts_across_keys_is_not_replayable(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    start(deps)
    start(deps, idempotency_key="other-key")
    with isolated_agent_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE agent.idempotency_key SET sealed_response=(SELECT sealed_response "
                "FROM agent.idempotency_key WHERE key='other-key') WHERE key='event-start-901'"
            )
        )
    before = facts(isolated_agent_database)
    with pytest.raises(AgentReplayUnavailable):
        start(deps)
    assert facts(isolated_agent_database) == before


@pytest.mark.parametrize("operation", ["start", "cancel", "resume"])
@pytest.mark.parametrize("replay_existing", [False, True])
def test_unavailable_key_fails_closed_for_first_execution_and_replay(
    isolated_agent_database: IsolatedAgentDatabase,
    operation: str,
    replay_existing: bool,
) -> None:
    class UnavailableSecrets:
        def load(self) -> SecretMaterial:
            raise SecretMaterialUnavailable("test-secret-detail-must-not-escape")

    deps = dependencies(isolated_agent_database)
    started = start(deps) if operation != "start" else None
    if operation == "resume":
        assert started is not None
        advance_to_running(deps, started.attempt.id)
        accept_workflow_event(None, event=waiting_event(started.attempt.id), dependencies=deps)

    def execute(candidate: AgentDependencies) -> StartRunResult | AttemptControlResult:
        if operation == "start":
            return start(candidate)
        assert started is not None
        if operation == "cancel":
            return cancel_attempt(
                None, command=cancel_command(started, revision=3), dependencies=candidate
            )
        return resume_attempt(
            None, command=resume_command(started, revision=6), dependencies=candidate
        )

    if replay_existing:
        execute(deps)
    before = facts(isolated_agent_database)
    with pytest.raises(AgentReplayUnavailable) as error:
        execute(replace(deps, secret_manager=UnavailableSecrets()))
    assert "test-secret-detail" not in str(error.value)
    assert facts(isolated_agent_database) == before
