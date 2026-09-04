from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

from sqlalchemy import Engine, text

from control_plane.app.modules.agent_run.adapters.sqlalchemy_repository import (
    SqlAlchemySandboxRepository,
    fencing_token_digest,
)
from control_plane.app.modules.agent_run.application.errors import SandboxAdmissionError
from control_plane.app.modules.agent_run.ports.repository import AdmissionPolicySnapshot
from tests.agent_run.factories import binding_projection

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
ENVIRONMENT_ID = "10000000-0000-0000-0000-000000000902"


def test_concurrent_reservations_create_exactly_one_active_lease_and_generation(
    isolated_agent_run_rw_engine: Engine,
) -> None:
    workers = 8
    barrier = Barrier(workers)
    policy = AdmissionPolicySnapshot(
        policy_version="sandbox-policy-v1",
        enabled=True,
        active_attempt_limit=8,
        maximum_units=8,
        lease_ttl_seconds=1800,
    )

    def reserve(index: int) -> str:
        barrier.wait()
        try:
            with isolated_agent_run_rw_engine.begin() as db:
                SqlAlchemySandboxRepository(db).reserve_materialization(
                    binding=binding_projection(
                        execution_id="execution-concurrent",
                        environment_id=ENVIRONMENT_ID,
                    ),
                    policy=policy,
                    materialization_id=str(uuid4()),
                    lease_id=str(uuid4()),
                    generation_id=str(uuid4()),
                    fencing_token_digest=fencing_token_digest(f"local-fence-token-{index}"),
                    now=NOW,
                )
            return "reserved"
        except SandboxAdmissionError as exc:
            return exc.code.value

    with ThreadPoolExecutor(max_workers=workers) as executor:
        outcomes = list(executor.map(reserve, range(workers)))

    with isolated_agent_run_rw_engine.connect() as db:
        active_materializations = db.execute(
            text(
                "SELECT count(*) FROM agent_run.sandbox_materialization "
                "WHERE state IN ('PROVISIONING','READY','QUARANTINED')"
            )
        ).scalar_one()
        active_leases = db.execute(
            text("SELECT count(*) FROM agent_run.capacity_lease WHERE state='ACTIVE'")
        ).scalar_one()
        active_generations = db.execute(
            text("SELECT count(*) FROM agent_run.runner_generation WHERE state='ACTIVE'")
        ).scalar_one()
        ledger = db.execute(
            text(
                "SELECT active_attempts, active_units FROM agent_run.capacity_ledger "
                "WHERE environment_id=:environment_id"
            ),
            {"environment_id": ENVIRONMENT_ID},
        ).one()

    assert outcomes.count("reserved") == 1
    assert active_materializations == active_leases == active_generations == 1
    assert ledger == (1, 1)


def test_cross_environment_provision_uses_one_execution_lock_domain(
    isolated_agent_run_rw_engine: Engine,
) -> None:
    barrier = Barrier(2)
    environments = (
        "10000000-0000-0000-0000-000000000921",
        "10000000-0000-0000-0000-000000000922",
    )
    policy = AdmissionPolicySnapshot(
        policy_version="sandbox-policy-v1",
        enabled=True,
        active_attempt_limit=1,
        maximum_units=1,
        lease_ttl_seconds=1800,
    )

    def reserve(environment_id: str) -> str:
        barrier.wait()
        try:
            binding = binding_projection(
                execution_id="execution-cross-environment",
                environment_id=environment_id,
            )
            binding = binding.model_copy(
                update={
                    "environment": binding.environment.model_copy(
                        update={
                            "workspace_id": f"workspace-{environment_id[-3:]}",
                            "requirement_id": f"requirement-{environment_id[-3:]}",
                        }
                    )
                }
            )
            with isolated_agent_run_rw_engine.begin() as db:
                SqlAlchemySandboxRepository(db).reserve_materialization(
                    binding=binding,
                    policy=policy,
                    materialization_id=str(uuid4()),
                    lease_id=str(uuid4()),
                    generation_id=str(uuid4()),
                    fencing_token_digest=fencing_token_digest(
                        f"local-fence-token-{environment_id}"
                    ),
                    now=NOW,
                )
            return "reserved"
        except SandboxAdmissionError as error:
            return f"{error.code.value}:{error.failure_dimension}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, environments))

    assert sorted(outcomes) == [
        "RUNTIME_BINDING_INVALID:active_execution",
        "reserved",
    ]
    with isolated_agent_run_rw_engine.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM agent_run.sandbox_materialization "
                    "WHERE execution_id='execution-cross-environment'"
                )
            ).scalar_one()
            == 1
        )
        assert db.execute(
            text(
                "SELECT COALESCE(sum(active_attempts), 0), COALESCE(sum(active_units), 0) "
                "FROM agent_run.capacity_ledger"
            )
        ).one() == (1, 1)
