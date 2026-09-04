from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from control_plane.app.modules.agent_run import (
    CommandContext,
    DenialCode,
    MaterializationBlocked,
    MaterializationFailed,
    MaterializationReady,
    ProvisionMaterializationCommand,
)
from control_plane.app.modules.agent_run.adapters import (
    RestrictedDevSandboxAdapter,
    SqlAlchemySandboxRepository,
)
from control_plane.app.modules.agent_run.application.controller import (
    RepositoryFactory,
    SandboxController,
    SandboxDependencies,
)
from control_plane.app.modules.agent_run.ports.repository import AdmissionPolicySnapshot
from control_plane.app.modules.agent_run.ports.runtime import (
    RuntimeMaterializationRequest,
    RuntimeMaterializerPort,
    RuntimeObservation,
    RuntimePreview,
    RuntimeReadiness,
)
from control_plane.app.modules.agent_run.ports.services import ClockPort, WorkloadAuthorizationPort
from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from tests.agent_run.conftest import IsolatedAgentRunDatabase
from tests.agent_run.factories import binding_projection


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class MutableClock(FixedClock):
    def advance(self, delta: timedelta) -> None:
        self._now += delta


class TestRandom:
    __test__ = False

    def __init__(self) -> None:
        self._token_number = 0

    def uuid4(self) -> UUID:
        return uuid4()

    def token_urlsafe(self, nbytes: int) -> str:
        self._token_number += 1
        return f"local-test-fence-{nbytes}-{self._token_number}"


class FixedAdmission:
    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

    def snapshot(self, environment_id: str) -> AdmissionPolicySnapshot:
        del environment_id
        return AdmissionPolicySnapshot(
            policy_version="sandbox-policy-v1",
            enabled=self._enabled,
            active_attempt_limit=1,
            maximum_units=1,
            lease_ttl_seconds=1800,
        )


class AllowAllAuthorization:
    def authorize(self, *, actor: str, operation: str, environment_id: str) -> bool:
        del actor, operation, environment_id
        return True


class WrongReadinessAdapter:
    def __init__(self, delegate: RestrictedDevSandboxAdapter) -> None:
        self.delegate = delegate

    def provision(self, request: RuntimeMaterializationRequest) -> RuntimeReadiness:
        readiness = self.delegate.provision(request)
        return readiness.model_copy(update={"generation": readiness.generation + 1})

    def publish_preview(self, *args: object, **kwargs: object) -> RuntimePreview:
        return self.delegate.publish_preview(*args, **kwargs)  # type: ignore[arg-type]

    def persist_evidence(self, *args: object, **kwargs: object) -> None:
        self.delegate.persist_evidence(*args, **kwargs)  # type: ignore[arg-type]

    def fence(self, *args: object, **kwargs: object) -> None:
        self.delegate.fence(*args, **kwargs)  # type: ignore[arg-type]

    def revoke_secret(self, *args: object, **kwargs: object) -> None:
        self.delegate.revoke_secret(*args, **kwargs)  # type: ignore[arg-type]

    def destroy(self, *args: object, **kwargs: object) -> None:
        self.delegate.destroy(*args, **kwargs)  # type: ignore[arg-type]

    def observe(self, materialization_id: str) -> RuntimeObservation:
        return self.delegate.observe(materialization_id)


def _command(
    now: datetime,
    *,
    key: str = "sandbox-provision-1",
    execution_id: str = "execution-provision-1",
    environment_id: str = "10000000-0000-0000-0000-000000000911",
) -> ProvisionMaterializationCommand:
    return ProvisionMaterializationCommand(
        context=CommandContext(
            idempotency_key=key,
            actor="workload:orchestrator",
            correlation_id=f"correlation:{key}",
            request_id=f"request:{key}",
        ),
        binding=binding_projection(
            execution_id=execution_id,
            environment_id=environment_id,
            deadline_at=now + timedelta(minutes=30),
        ),
    )


def _runtime(tmp_path: Path) -> RestrictedDevSandboxAdapter:
    repository = tmp_path / "repository-1"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
    )


def _controller(
    engine: Engine,
    runtime: RuntimeMaterializerPort,
    now: datetime,
    *,
    admission: FixedAdmission | None = None,
    authorization: WorkloadAuthorizationPort | None = None,
    clock: ClockPort | None = None,
    repository_factory: RepositoryFactory = SqlAlchemySandboxRepository,
) -> SandboxController:
    return SandboxController(
        SandboxDependencies(
            engine=engine,
            repository_factory=repository_factory,
            runtime=runtime,
            admission=admission or FixedAdmission(),
            authorization=authorization or AllowAllAuthorization(),
            audit=SqlAlchemyTransactionalAuditAppender(),
            clock=clock or FixedClock(now),
            random=TestRandom(),
            idempotency_sealing_key=bytes(range(32)),
        )
    )


def test_provision_reaches_ready_after_matching_handshake_and_replays_exactly(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)
    command = _command(now)

    first = controller.provision_materialization(command)
    replay = controller.provision_materialization(command)

    assert isinstance(first, MaterializationReady)
    assert replay == first
    assert first.handle.revision == 2
    assert first.handle.generation == 1
    assert first.lab_only is True
    assert [event.action for event in runtime.events] == ["provision"]
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text("SELECT state, revision FROM agent_run.sandbox_materialization")
        ).one() == ("READY", 2)
        assert db.execute(
            text("SELECT state, ready_at IS NOT NULL FROM agent_run.runner_generation")
        ).one() == ("ACTIVE", True)
        audit = db.execute(
            text(
                "SELECT action, result, reason FROM audit.audit_event "
                "WHERE target_id=:target_id ORDER BY occurred_at, id"
            ),
            {"target_id": first.handle.materialization_id},
        ).all()
    assert ("sandbox.materialization.provision", "SUCCEEDED", None) in audit


def test_disabled_policy_and_expired_binding_are_canonical_audited_denials(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    runtime = _runtime(tmp_path)
    disabled = _controller(
        isolated_agent_run_database.runtime,
        runtime,
        now,
        admission=FixedAdmission(enabled=False),
    ).provision_materialization(_command(now, key="sandbox-policy-disabled"))
    expired_command = _command(
        now,
        key="sandbox-expired-binding",
        execution_id="execution-expired",
        environment_id="10000000-0000-0000-0000-000000000912",
    )
    expired_command = expired_command.model_copy(
        update={
            "binding": expired_command.binding.model_copy(
                update={"deadline_at": now - timedelta(seconds=1)}
            )
        }
    )
    expired = _controller(
        isolated_agent_run_database.runtime,
        runtime,
        now,
    ).provision_materialization(expired_command)

    assert isinstance(disabled, MaterializationBlocked)
    assert disabled.denial.code is DenialCode.POLICY_DISABLED
    assert isinstance(expired, MaterializationBlocked)
    assert expired.denial.code is DenialCode.RUNTIME_BINDING_INVALID
    assert expired.denial.failure_dimension == "deadline"
    with isolated_agent_run_database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM agent_run.sandbox_materialization")).scalar_one()
            == 0
        )
        reasons = set(
            db.execute(
                text(
                    "SELECT reason FROM audit.audit_event "
                    "WHERE action='sandbox.materialization.provision'"
                )
            ).scalars()
        )
    assert "POLICY_DISABLED:admission_policy" in reasons
    assert "RUNTIME_BINDING_INVALID:deadline" in reasons


def test_bad_readiness_is_failed_cleaned_and_never_leaks_sensitive_values_to_audit(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    delegate = _runtime(tmp_path)
    controller = _controller(
        isolated_agent_run_database.runtime,
        WrongReadinessAdapter(delegate),
        now,
    )

    result = controller.provision_materialization(_command(now, key="sandbox-bad-readiness"))

    assert isinstance(result, MaterializationFailed)
    assert result.denial.code is DenialCode.RUNTIME_BINDING_INVALID
    assert result.denial.failure_dimension == "runner_readiness"
    assert [event.action for event in delegate.events] == [
        "provision",
        "evidence",
        "fence",
        "revoke_secret",
        "destroy",
    ]
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT state, evidence_persisted_at IS NOT NULL, fenced_at IS NOT NULL, "
                "secret_revoked_at IS NOT NULL, lease_released_at IS NOT NULL, "
                "destroyed_at IS NOT NULL FROM agent_run.sandbox_materialization"
            )
        ).one() == ("FAILED", True, True, True, True, True)
        assert db.execute(
            text("SELECT active_attempts, active_units FROM agent_run.capacity_ledger")
        ).one() == (0, 0)
        audit_text = " ".join(
            str(value)
            for row in db.execute(
                text(
                    "SELECT actor, action, target_type, target_id, result, reason, "
                    "correlation_id FROM audit.audit_event"
                )
            )
            for value in row
        ).lower()
    assert "local-test-fence" not in audit_text
    assert "secret-lease" not in audit_text
    assert "provider" not in audit_text
    assert "pod" not in audit_text


def test_runtime_materialization_failure_is_retryable_and_uses_cleanup_chain(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    repository = tmp_path / "repository-1"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runtime = RestrictedDevSandboxAdapter(
        repository_root=tmp_path,
        repositories={"repository-1": repository},
        fail_steps=frozenset({"provision"}),
    )
    controller = _controller(isolated_agent_run_database.runtime, runtime, now)

    result = controller.provision_materialization(
        _command(now, key="sandbox-runtime-materialization-failure")
    )

    assert isinstance(result, MaterializationFailed)
    assert result.denial.code is DenialCode.RESOURCE_EXHAUSTED
    assert result.denial.failure_dimension == "runtime_materialization"
    assert result.denial.retryable is True
    with isolated_agent_run_database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT state, evidence_persisted_at IS NOT NULL, fenced_at IS NOT NULL, "
                "secret_revoked_at IS NOT NULL, lease_released_at IS NOT NULL, "
                "destroyed_at IS NOT NULL FROM agent_run.sandbox_materialization"
            )
        ).one() == ("FAILED", True, True, True, True, True)
        assert db.execute(
            text("SELECT active_attempts, active_units FROM agent_run.capacity_ledger")
        ).one() == (0, 0)
