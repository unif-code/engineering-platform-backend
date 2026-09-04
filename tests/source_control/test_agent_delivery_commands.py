from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Lock
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control import (
    AgentDeliveryDependencyUnavailable,
    AgentExecutionBindingSnapshot,
    AgentPushBindingRejected,
    AgentPushIdempotencyConflict,
    AgentPushRequestSpec,
    AgentPushState,
    SourceControlDependencies,
    authorize_agent_push,
    execute_agent_push,
)
from control_plane.app.modules.source_control.adapters import (
    DevBrokerBehavior,
    RestrictedDevAgentPushBroker,
    SqlAlchemyAgentDeliveryRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.domain import digest_agent_push_grant
from control_plane.app.modules.source_control.ports import IssuedAgentPushGrant
from tests.source_control.test_agent_delivery_adapters import (
    ATTEMPT_ID,
    BINDING_DIGEST,
    BRANCH_BINDING_ID,
    BRANCH_NAME,
    CONTENT_DIGEST,
    EXPECTED_HEAD,
    NOW,
    REPOSITORY_ID,
    REQUIREMENT_ID,
    TARGET_COMMIT,
    WORK_ITEM_ID,
    WORKSPACE_ID,
    _seed_branch,
)

RAW_GRANT = "v10-one-time-grant-sentinel-never-persist"
RAW_FENCE = "v10-fencing-sentinel-never-persist"


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class ThreadSafeRandom:
    def __init__(self) -> None:
        self._next = 500
        self._lock = Lock()

    def uuid4(self) -> UUID:
        with self._lock:
            self._next += 1
            value = self._next
        return UUID(f"94000000-0000-0000-0000-{value:012d}")


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[AuditEnvelope] = []
        self._lock = Lock()

    def append_in_transaction(self, _db: object, envelope: AuditEnvelope) -> None:
        with self._lock:
            self.events.append(envelope)


class FixedGrantIssuer:
    def __init__(self, raw: str = RAW_GRANT) -> None:
        self.raw = raw
        self.calls = 0

    def issue(self) -> IssuedAgentPushGrant:
        self.calls += 1
        return IssuedAgentPushGrant(
            raw=self.raw,
            digest=digest_agent_push_grant(self.raw),
        )


class FixedAgentPolicy:
    def max_push_grant_ttl(self) -> timedelta:
        return timedelta(minutes=2)

    def next_reconcile_at(self, *, now: datetime, attempts: int) -> datetime:
        return now + timedelta(seconds=min(15 * max(attempts, 1), 60))


class FixedExecutionBindings:
    def __init__(self, *, active: bool = True, fenced: bool = False) -> None:
        self.active = active
        self.fenced = fenced
        self.calls = 0

    def validate(
        self,
        spec: AgentPushRequestSpec,
        *,
        raw_fencing_token: str,
    ) -> AgentExecutionBindingSnapshot:
        self.calls += 1
        assert raw_fencing_token == RAW_FENCE
        return AgentExecutionBindingSnapshot(
            attempt_id=spec.attempt_id,
            attempt_generation=spec.attempt_generation,
            execution_binding_digest=BINDING_DIGEST,
            requirement_id=spec.requirement_id,
            work_item_id=spec.work_item_id,
            workspace_id=spec.workspace_id,
            repository_id=spec.repository_id,
            branch_binding_id=spec.branch_binding_id,
            branch_name=spec.branch_name,
            active=self.active,
            fenced=self.fenced,
        )


def _spec(**overrides: object) -> AgentPushRequestSpec:
    values: dict[str, object] = {
        "idempotency_key": "agent-push-command-301",
        "correlation_id": "correlation-command-301",
        "attempt_id": ATTEMPT_ID,
        "attempt_generation": 3,
        "requirement_id": REQUIREMENT_ID,
        "work_item_id": WORK_ITEM_ID,
        "workspace_id": WORKSPACE_ID,
        "repository_id": REPOSITORY_ID,
        "branch_binding_id": BRANCH_BINDING_ID,
        "branch_name": BRANCH_NAME,
        "expected_remote_head_sha": EXPECTED_HEAD,
        "target_commit_sha": TARGET_COMMIT,
        "content_digest": CONTENT_DIGEST,
        "artifact_refs": ("artifact://patch/301",),
        "expires_at": NOW + timedelta(seconds=60),
    }
    values.update(overrides)
    return AgentPushRequestSpec.model_validate(values)


def _dependencies(
    engine: Engine,
    *,
    broker: RestrictedDevAgentPushBroker | None = None,
    bindings: FixedExecutionBindings | None = None,
    issuer: FixedGrantIssuer | None = None,
    clock: MutableClock | None = None,
) -> tuple[
    SourceControlDependencies,
    RestrictedDevAgentPushBroker,
    FixedExecutionBindings,
    FixedGrantIssuer,
    MutableClock,
    FakeAudit,
]:
    resolved_broker = broker or RestrictedDevAgentPushBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
    )
    resolved_bindings = bindings or FixedExecutionBindings()
    resolved_issuer = issuer or FixedGrantIssuer()
    resolved_clock = clock or MutableClock()
    audit = FakeAudit()
    return (
        SourceControlDependencies(
            repository_factory=SqlAlchemySourceControlRepository,
            engine=engine,
            requirement=None,
            eligibility=None,
            audit=audit,
            clock=resolved_clock,
            random=ThreadSafeRandom(),
            agent_delivery_repository_factory=SqlAlchemyAgentDeliveryRepository,
            agent_execution_bindings=resolved_bindings,
            agent_push_broker=resolved_broker,
            agent_push_grants=resolved_issuer,
            agent_delivery_policy=FixedAgentPolicy(),
        ),
        resolved_broker,
        resolved_bindings,
        resolved_issuer,
        resolved_clock,
        audit,
    )


def test_authorize_agent_push_persists_only_digest_and_appends_safe_audit(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    dependencies, _broker, _bindings, issuer, _clock, audit = _dependencies(
        isolated_source_control_rw_engine
    )

    result = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    assert result.raw_grant == RAW_GRANT
    assert result.replayed is False
    assert result.delivery.state is AgentPushState.AUTHORIZED
    assert RAW_GRANT not in repr(result)
    assert RAW_FENCE not in repr(result)
    assert issuer.calls == 1
    with isolated_source_control_rw_engine.connect() as db:
        row_text = db.execute(
            text(
                "SELECT row_to_json(request)::text FROM "
                "source_control.agent_push_request AS request WHERE id=:request_id"
            ),
            {"request_id": result.delivery.id},
        ).scalar_one()
    assert RAW_GRANT not in row_text
    assert RAW_FENCE not in row_text
    assert digest_agent_push_grant(RAW_GRANT) in row_text
    assert [event.action for event in audit.events] == ["source_control.agent_push_authorized"]


def test_authorization_is_idempotent_without_reissuing_raw_grant(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    dependencies, _broker, _bindings, issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine
    )

    first = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    replay = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    assert replay.delivery.id == first.delivery.id
    assert replay.raw_grant is None
    assert replay.replayed is True
    assert issuer.calls == 1

    with pytest.raises(AgentPushIdempotencyConflict):
        authorize_agent_push(
            _spec(target_commit_sha="c" * 40),
            raw_fencing_token=RAW_FENCE,
            dependencies=dependencies,
        )


def test_authorization_rejects_source_control_binding_before_token_validation(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    dependencies, _broker, bindings, issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine
    )

    with pytest.raises(AgentPushBindingRejected):
        authorize_agent_push(
            _spec(workspace_id="20000000-0000-0000-0000-000000000399"),
            raw_fencing_token=RAW_FENCE,
            dependencies=dependencies,
        )

    assert bindings.calls == 0
    assert issuer.calls == 0


def test_authorization_fails_closed_when_agent_delivery_dependency_is_missing(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    dependencies, _broker, _bindings, _issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine
    )
    unavailable = SourceControlDependencies(
        repository_factory=dependencies.repository_factory,
        engine=dependencies.engine,
        requirement=None,
        eligibility=None,
        audit=dependencies.audit,
        clock=dependencies.clock,
        random=dependencies.random,
    )

    with pytest.raises(AgentDeliveryDependencyUnavailable):
        authorize_agent_push(
            _spec(),
            raw_fencing_token=RAW_FENCE,
            dependencies=unavailable,
        )


def test_expired_grant_blocks_without_calling_broker(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    clock = MutableClock()
    dependencies, broker, _bindings, _issuer, _clock, audit = _dependencies(
        isolated_source_control_rw_engine,
        clock=clock,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    clock.value = NOW + timedelta(seconds=61)

    result = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    assert result.state is AgentPushState.BLOCKED
    assert result.last_error_code == "GRANT_EXPIRED"
    assert broker.push_count == 0
    assert audit.events[-1].action == "source_control.agent_push_blocked"


def test_success_and_repeated_execution_produce_one_broker_write_and_one_fact(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    dependencies, broker, bindings, _issuer, _clock, audit = _dependencies(
        isolated_source_control_rw_engine
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    first = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    replay = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    assert first.state is AgentPushState.SUCCEEDED
    assert replay == first
    assert broker.push_count == 1
    assert broker.write_count == 1
    assert bindings.calls == 3
    with isolated_source_control_rw_engine.connect() as db:
        fact_count = db.execute(
            text(
                "SELECT count(*) FROM source_control.agent_delivery_fact "
                "WHERE push_request_id=:request_id"
            ),
            {"request_id": first.id},
        ).scalar_one()
    assert fact_count == 1
    assert "source_control.agent_push_confirmed" in {event.action for event in audit.events}


def test_post_broker_binding_rejection_fences_without_confirming_delivery(
    isolated_source_control_rw_engine: Engine,
) -> None:
    class RejectAfterBrokerWriteBindings(FixedExecutionBindings):
        def validate(
            self,
            spec: AgentPushRequestSpec,
            *,
            raw_fencing_token: str,
        ) -> AgentExecutionBindingSnapshot:
            snapshot = super().validate(
                spec,
                raw_fencing_token=raw_fencing_token,
            )
            if self.calls == 3:
                raise AgentPushBindingRejected("Agent push binding was rejected")
            return snapshot

    _seed_branch(isolated_source_control_rw_engine)
    bindings = RejectAfterBrokerWriteBindings()
    dependencies, broker, _bindings, _issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine,
        bindings=bindings,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    result = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    assert result.state is AgentPushState.FENCED
    assert broker.write_count == 1
    assert (REPOSITORY_ID, BRANCH_NAME) in broker.frozen_branches
    with isolated_source_control_rw_engine.connect() as db:
        topics = (
            db.execute(
                text(
                    "SELECT topic FROM source_control.agent_delivery_fact "
                    "WHERE push_request_id=:request_id"
                ),
                {"request_id": grant.delivery.id},
            )
            .scalars()
            .all()
        )
    assert topics == ["source-control.agent-push-fenced.v1"]


def test_broker_unknown_is_never_success_and_emits_no_confirmed_fact(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    broker = RestrictedDevAgentPushBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
    )
    dependencies, _broker, _bindings, _issuer, _clock, audit = _dependencies(
        isolated_source_control_rw_engine,
        broker=broker,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    broker.set_behavior(grant.delivery.id, DevBrokerBehavior.UNKNOWN_AFTER_WRITE)

    result = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    assert result.state is AgentPushState.UNKNOWN
    assert result.last_error_code == "RESULT_UNKNOWN"
    assert broker.write_count == 1
    with isolated_source_control_rw_engine.connect() as db:
        fact_count = db.execute(
            text("SELECT count(*) FROM source_control.agent_delivery_fact"),
        ).scalar_one()
    assert fact_count == 0
    assert audit.events[-1].action == "source_control.agent_push_unknown"


@pytest.mark.parametrize(
    ("behavior", "expected_error"),
    [
        (DevBrokerBehavior.DENIED, "BROKER_DENIED"),
        (DevBrokerBehavior.SUCCESS, "REMOTE_HEAD_CONFLICT"),
    ],
)
def test_safe_broker_denial_and_head_conflict_are_terminal_blocked(
    isolated_source_control_rw_engine: Engine,
    behavior: DevBrokerBehavior,
    expected_error: str,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    initial_head = EXPECTED_HEAD if behavior is DevBrokerBehavior.DENIED else "f" * 40
    broker = RestrictedDevAgentPushBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): initial_head},
    )
    dependencies, _broker, _bindings, _issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine,
        broker=broker,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    broker.set_behavior(grant.delivery.id, behavior)

    result = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    assert result.state is AgentPushState.BLOCKED
    assert result.last_error_code == expected_error
    assert broker.write_count == 0


def test_concurrent_execution_performs_exactly_one_broker_push(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    dependencies, broker, _bindings, _issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    def execute() -> AgentPushState:
        return execute_agent_push(
            grant.delivery.id,
            raw_grant=RAW_GRANT,
            raw_fencing_token=RAW_FENCE,
            dependencies=dependencies,
        ).state

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = tuple(pool.map(lambda _index: execute(), range(2)))

    with isolated_source_control_rw_engine.connect() as db:
        stored_state = db.execute(
            text("SELECT state FROM source_control.agent_push_request WHERE id=:id"),
            {"id": grant.delivery.id},
        ).scalar_one()
    assert AgentPushState.SUCCEEDED in states
    assert stored_state == AgentPushState.SUCCEEDED.value
    assert broker.push_count == 1
    assert broker.write_count == 1
