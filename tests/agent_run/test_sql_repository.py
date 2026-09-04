from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.errors import UniqueViolation
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.agent_run import MaterializationGuard
from control_plane.app.modules.agent_run.adapters.sqlalchemy_repository import (
    SqlAlchemySandboxRepository,
    fencing_token_digest,
)
from control_plane.app.modules.agent_run.application.errors import StaleRunnerGeneration
from control_plane.app.modules.agent_run.ports.repository import (
    AdmissionPolicySnapshot,
    ReservationRecord,
)
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from tests.agent_run.factories import binding_projection

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
SEALING_KEY = bytes(range(32))
ENVIRONMENT_ID = "10000000-0000-0000-0000-000000000901"


def _policy(
    *,
    enabled: bool = True,
    active_attempt_limit: int = 1,
    maximum_units: int = 1,
) -> AdmissionPolicySnapshot:
    return AdmissionPolicySnapshot(
        policy_version="sandbox-policy-v1",
        enabled=enabled,
        active_attempt_limit=active_attempt_limit,
        maximum_units=maximum_units,
        lease_ttl_seconds=1800,
    )


def _reserve(
    repository: SqlAlchemySandboxRepository,
    *,
    execution_id: str = "execution-1",
    token: str = "local-test-fencing-token",
) -> ReservationRecord:
    return repository.reserve_materialization(
        binding=binding_projection(
            execution_id=execution_id,
            environment_id=ENVIRONMENT_ID,
        ),
        policy=_policy(),
        materialization_id=str(uuid4()),
        lease_id=str(uuid4()),
        generation_id=str(uuid4()),
        fencing_token_digest=fencing_token_digest(token),
        now=NOW,
    )


def test_environment_and_capacity_ledger_creation_are_idempotent(
    isolated_agent_run_rw_engine: Engine,
) -> None:
    binding = binding_projection(environment_id=ENVIRONMENT_ID)
    with isolated_agent_run_rw_engine.begin() as db:
        repository = SqlAlchemySandboxRepository(db)
        repository.ensure_environment_and_capacity(binding.environment, _policy(), now=NOW)
        repository.ensure_environment_and_capacity(binding.environment, _policy(), now=NOW)

    with isolated_agent_run_rw_engine.connect() as db:
        environment_count = db.execute(
            text("SELECT count(*) FROM agent_run.sandbox_environment")
        ).scalar_one()
        ledger_count = db.execute(
            text("SELECT count(*) FROM agent_run.capacity_ledger")
        ).scalar_one()

    assert environment_count == 1
    assert ledger_count == 1


def test_unrelated_materialization_primary_key_error_is_not_mapped_to_active_conflict(
    isolated_agent_run_rw_engine: Engine,
) -> None:
    materialization_id = str(uuid4())
    policy = _policy(active_attempt_limit=2, maximum_units=2)

    def reserve(execution_id: str) -> None:
        with isolated_agent_run_rw_engine.begin() as db:
            SqlAlchemySandboxRepository(db).reserve_materialization(
                binding=binding_projection(
                    execution_id=execution_id, environment_id=ENVIRONMENT_ID
                ),
                policy=policy,
                materialization_id=materialization_id,
                lease_id=str(uuid4()),
                generation_id=str(uuid4()),
                fencing_token_digest=fencing_token_digest("local-constraint-test"),
                now=NOW,
            )

    reserve("execution-primary-key-1")
    with pytest.raises(IntegrityError) as error:
        reserve("execution-primary-key-2")
    assert isinstance(error.value.orig, UniqueViolation)
    assert error.value.orig.diag.constraint_name == "sandbox_materialization_pkey"
    with isolated_agent_run_rw_engine.connect() as db:
        assert db.execute(
            text("SELECT active_attempts, active_units FROM agent_run.capacity_ledger")
        ).one() == (1, 1)


def test_command_receipt_replays_authenticated_ciphertext_and_rejects_key_rebinding(
    isolated_agent_run_rw_engine: Engine,
) -> None:
    fingerprint = canonical_request_fingerprint(
        operation="sandbox.provision",
        method="POST",
        path="/api/v1/internal/sandbox/materializations",
        body={"executionId": "execution-1"},
        idempotency_sealing_key=SEALING_KEY,
    )
    token = "local-test-fencing-token"

    with isolated_agent_run_rw_engine.begin() as db:
        first = execute_idempotent(
            SqlAlchemySandboxRepository(db),
            actor="workload:orchestrator",
            operation="sandbox.provision",
            key="sandbox-replay-1",
            fingerprint=fingerprint,
            command=lambda: IdempotentResponse(
                status_code=201,
                body={"fencingToken": token, "revision": 1},
            ),
            now=lambda: NOW,
            new_id=uuid4,
            idempotency_sealing_key=SEALING_KEY,
        )
    with isolated_agent_run_rw_engine.begin() as db:
        replay = execute_idempotent(
            SqlAlchemySandboxRepository(db),
            actor="workload:orchestrator",
            operation="sandbox.provision",
            key="sandbox-replay-1",
            fingerprint=fingerprint,
            command=lambda: pytest.fail("replay must not execute the command"),
            now=lambda: NOW,
            new_id=uuid4,
            idempotency_sealing_key=SEALING_KEY,
        )
        ciphertext = db.execute(
            text(
                "SELECT sealed_response FROM agent_run.command_receipt "
                "WHERE idempotency_key='sandbox-replay-1'"
            )
        ).scalar_one()

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.response == first.response
    assert token.encode("utf-8") not in ciphertext

    different = canonical_request_fingerprint(
        operation="sandbox.provision",
        method="POST",
        path="/api/v1/internal/sandbox/materializations",
        body={"executionId": "execution-2"},
        idempotency_sealing_key=SEALING_KEY,
    )
    with (
        isolated_agent_run_rw_engine.begin() as db,
        pytest.raises(IdempotencyConflict),
    ):
        execute_idempotent(
            SqlAlchemySandboxRepository(db),
            actor="workload:orchestrator",
            operation="sandbox.provision",
            key="sandbox-replay-1",
            fingerprint=different,
            command=lambda: pytest.fail("conflicting command must not execute"),
            now=lambda: NOW,
            new_id=uuid4,
            idempotency_sealing_key=SEALING_KEY,
        )


def test_expired_command_owner_is_taken_over_and_stale_owner_cannot_complete(
    isolated_agent_run_rw_engine: Engine,
) -> None:
    command_id = str(uuid4())
    first_owner = str(uuid4())
    takeover_owner = str(uuid4())
    fingerprint = "a" * 64

    with isolated_agent_run_rw_engine.begin() as db:
        first = SqlAlchemySandboxRepository(db).acquire_command(
            command_id=command_id,
            owner_id=first_owner,
            actor="workload:orchestrator",
            operation="sandbox.provision",
            idempotency_key="sandbox-owner-takeover",
            request_fingerprint=fingerprint,
            now=NOW,
            owner_expires_at=NOW + timedelta(seconds=30),
        )
    assert first.created is True
    assert first.taken_over is False
    assert first.command_id == command_id

    with (
        isolated_agent_run_rw_engine.begin() as db,
        pytest.raises(IdempotencyConflict, match="live owner"),
    ):
        SqlAlchemySandboxRepository(db).acquire_command(
            command_id=str(uuid4()),
            owner_id=takeover_owner,
            actor="workload:orchestrator",
            operation="sandbox.provision",
            idempotency_key="sandbox-owner-takeover",
            request_fingerprint=fingerprint,
            now=NOW + timedelta(seconds=10),
            owner_expires_at=NOW + timedelta(seconds=40),
        )

    with isolated_agent_run_rw_engine.begin() as db:
        takeover = SqlAlchemySandboxRepository(db).acquire_command(
            command_id=str(uuid4()),
            owner_id=takeover_owner,
            actor="workload:orchestrator",
            operation="sandbox.provision",
            idempotency_key="sandbox-owner-takeover",
            request_fingerprint=fingerprint,
            now=NOW + timedelta(seconds=31),
            owner_expires_at=NOW + timedelta(seconds=61),
        )
    assert takeover.created is False
    assert takeover.taken_over is True
    assert takeover.command_id == command_id
    assert takeover.owner_id == takeover_owner

    response = b"sealed-local-test-response"
    with isolated_agent_run_rw_engine.begin() as db:
        repository = SqlAlchemySandboxRepository(db)
        assert (
            repository.complete_idempotency(
                command_id,
                owner_id=first_owner,
                http_status=201,
                result_metadata={"kind": "http-response", "schemaVersion": 1},
                sealed_response=response,
                now=NOW + timedelta(seconds=32),
            )
            is False
        )
        assert (
            repository.complete_idempotency(
                command_id,
                owner_id=takeover_owner,
                http_status=201,
                result_metadata={"kind": "http-response", "schemaVersion": 1},
                sealed_response=response,
                now=NOW + timedelta(seconds=32),
            )
            is True
        )


def test_reservation_persists_one_lease_generation_and_only_a_fence_digest(
    isolated_agent_run_rw_engine: Engine,
) -> None:
    token = "local-test-fencing-token"
    with isolated_agent_run_rw_engine.begin() as db:
        reservation = _reserve(SqlAlchemySandboxRepository(db), token=token)

    with isolated_agent_run_rw_engine.connect() as db:
        materialization = db.execute(
            text("SELECT state, revision, generation FROM agent_run.sandbox_materialization")
        ).one()
        lease = db.execute(text("SELECT state, unit_weight FROM agent_run.capacity_lease")).one()
        generation = db.execute(
            text("SELECT state, fencing_token_digest FROM agent_run.runner_generation")
        ).one()

    assert reservation.generation == 1
    assert materialization == ("PROVISIONING", 1, 1)
    assert lease == ("ACTIVE", 1)
    assert generation.state == "ACTIVE"
    assert generation.fencing_token_digest == fencing_token_digest(token)
    assert generation.fencing_token_digest != token


def test_stale_fence_or_revision_cannot_lock_current_generation(
    isolated_agent_run_rw_engine: Engine,
) -> None:
    with isolated_agent_run_rw_engine.begin() as db:
        reservation = _reserve(SqlAlchemySandboxRepository(db))

    stale = MaterializationGuard(
        materialization_id=reservation.materialization_id,
        lease_id=reservation.lease_id,
        generation=reservation.generation,
        fencing_token=SecretStr("wrong-local-test-fencing-token"),
        expected_revision=reservation.revision,
    )
    with (
        isolated_agent_run_rw_engine.begin() as db,
        pytest.raises(StaleRunnerGeneration),
    ):
        SqlAlchemySandboxRepository(db).lock_current_guard(stale)

    with isolated_agent_run_rw_engine.connect() as db:
        assert db.execute(
            text("SELECT state, revision FROM agent_run.sandbox_materialization")
        ).one() == ("PROVISIONING", 1)
