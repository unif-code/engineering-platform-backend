from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

from control_plane.app.modules.source_control import (
    AgentPushBindingRejected,
    AgentPushState,
    authorize_agent_push,
    execute_agent_push,
    fence_agent_attempt,
    reconcile_agent_pushes,
    reconcile_agent_revocations,
)
from control_plane.app.modules.source_control.adapters import (
    DevBrokerBehavior,
    RestrictedDevAgentPushBroker,
    SqlAlchemyAgentDeliveryRepository,
)
from control_plane.app.modules.source_control.ports import (
    AgentPushResultUnknown,
    BrokerGrantLocator,
    BrokerPushObservation,
    BrokerPushRequest,
    BrokerRevocationResult,
)
from tests.source_control.test_agent_delivery_adapters import (
    ATTEMPT_ID,
    BRANCH_NAME,
    EXPECTED_HEAD,
    NOW,
    REPOSITORY_ID,
    TARGET_COMMIT,
    _seed_branch,
)
from tests.source_control.test_agent_delivery_commands import (
    RAW_FENCE,
    RAW_GRANT,
    FixedExecutionBindings,
    _dependencies,
    _spec,
)


def _fact_topics(engine: Engine) -> tuple[str, ...]:
    with engine.connect() as db:
        return tuple(
            db.execute(
                text("SELECT topic FROM source_control.agent_delivery_fact ORDER BY topic")
            ).scalars()
        )


def _stored_request(engine: Engine, request_id: str) -> Mapping[str, Any]:
    with engine.connect() as db:
        return dict(
            db.execute(
                text("SELECT * FROM source_control.agent_push_request WHERE id=:id"),
                {"id": request_id},
            )
            .mappings()
            .one()
        )


def test_unknown_after_write_reconciles_by_observation_without_repeating_push(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    broker = RestrictedDevAgentPushBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
    )
    dependencies, _broker, _bindings, _issuer, clock, _audit = _dependencies(
        isolated_source_control_rw_engine,
        broker=broker,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    broker.set_behavior(grant.delivery.id, DevBrokerBehavior.UNKNOWN_AFTER_WRITE)
    unknown = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    clock.value = NOW + timedelta(seconds=16)

    batch = reconcile_agent_pushes(limit=10, dependencies=dependencies)

    assert unknown.state is AgentPushState.UNKNOWN
    assert batch.claimed == 1
    assert batch.processed == 1
    assert batch.deliveries[0].state is AgentPushState.SUCCEEDED
    assert broker.push_count == 1
    assert broker.write_count == 1
    assert broker.observe_count == 1
    assert _fact_topics(isolated_source_control_rw_engine) == (
        "source-control.agent-push-confirmed.v1",
    )


def test_unknown_before_write_with_expected_old_head_stays_unknown(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    broker = RestrictedDevAgentPushBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
    )
    dependencies, _broker, _bindings, _issuer, clock, _audit = _dependencies(
        isolated_source_control_rw_engine,
        broker=broker,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    broker.set_behavior(grant.delivery.id, DevBrokerBehavior.UNKNOWN_BEFORE_WRITE)
    execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    clock.value = NOW + timedelta(seconds=16)

    batch = reconcile_agent_pushes(limit=10, dependencies=dependencies)
    stored = _stored_request(isolated_source_control_rw_engine, grant.delivery.id)

    assert batch.deliveries[0].state is AgentPushState.UNKNOWN
    assert broker.push_count == 1
    assert broker.write_count == 0
    assert broker.observe_count == 1
    assert stored["next_reconcile_at"] > clock.value
    assert _fact_topics(isolated_source_control_rw_engine) == ()


def test_unknown_observing_third_party_head_is_terminal_conflict(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    broker = RestrictedDevAgentPushBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
    )
    dependencies, _broker, _bindings, _issuer, clock, _audit = _dependencies(
        isolated_source_control_rw_engine,
        broker=broker,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    broker.set_behavior(grant.delivery.id, DevBrokerBehavior.UNKNOWN_BEFORE_WRITE)
    execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    broker.seed_remote_head(REPOSITORY_ID, BRANCH_NAME, "f" * 40)
    clock.value = NOW + timedelta(seconds=16)

    batch = reconcile_agent_pushes(limit=10, dependencies=dependencies)

    assert batch.deliveries[0].state is AgentPushState.BLOCKED
    assert batch.deliveries[0].last_error_code == "REMOTE_HEAD_CONFLICT"
    assert broker.push_count == 1
    assert _fact_topics(isolated_source_control_rw_engine) == ()


def test_fence_is_committed_before_broker_revocation_and_blocks_execution(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)

    class CommitCheckingBroker(RestrictedDevAgentPushBroker):
        def revoke(self, locator: BrokerGrantLocator) -> BrokerRevocationResult:
            with isolated_source_control_rw_engine.connect() as db:
                request_state, fence_generation = db.execute(
                    text(
                        "SELECT request.state, fence.fenced_generation "
                        "FROM source_control.agent_push_request AS request "
                        "JOIN source_control.agent_delivery_fence AS fence "
                        "ON fence.attempt_id=request.attempt_id WHERE request.attempt_id=:id"
                    ),
                    {"id": ATTEMPT_ID},
                ).one()
            assert request_state == "FENCED"
            assert fence_generation == 3
            return super().revoke(locator)

    broker = CommitCheckingBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
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

    fenced = fence_agent_attempt(
        attempt_id=ATTEMPT_ID,
        fenced_generation=3,
        reason_code="ATTEMPT_CANCELLED",
        correlation_id="correlation-fence-401",
        dependencies=dependencies,
    )
    delivery = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    assert fenced.revocation_state == "SUCCEEDED"
    assert [item.state for item in fenced.deliveries] == [AgentPushState.FENCED]
    assert delivery.state is AgentPushState.FENCED
    assert broker.push_count == 0


def test_stale_fence_replay_uses_canonical_metadata_without_audit_token_leak(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    dependencies, _broker, _bindings, _issuer, _clock, audit = _dependencies(
        isolated_source_control_rw_engine
    )
    authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    fence_agent_attempt(
        attempt_id=ATTEMPT_ID,
        fenced_generation=3,
        reason_code="ATTEMPT_CANCELLED",
        correlation_id="correlation-canonical-fence-401",
        dependencies=dependencies,
    )

    replay = fence_agent_attempt(
        attempt_id=ATTEMPT_ID,
        fenced_generation=2,
        reason_code=RAW_FENCE,
        correlation_id=RAW_GRANT,
        dependencies=dependencies,
    )

    replay_audit = audit.events[-1]
    assert replay.fenced_generation == 3
    assert replay_audit.action == "source_control.agent_attempt_fenced"
    assert replay_audit.reason == "ATTEMPT_CANCELLED"
    assert replay_audit.correlation_id == "correlation-canonical-fence-401"
    assert RAW_GRANT not in repr(audit.events)
    assert RAW_FENCE not in repr(audit.events)


def test_stale_execution_binding_is_rejected_before_broker_push(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    bindings = FixedExecutionBindings()
    dependencies, broker, _bindings, _issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine,
        bindings=bindings,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    bindings.fenced = True

    with pytest.raises(AgentPushBindingRejected):
        execute_agent_push(
            grant.delivery.id,
            raw_grant=RAW_GRANT,
            raw_fencing_token=RAW_FENCE,
            dependencies=dependencies,
        )

    assert broker.push_count == 0
    assert (
        _stored_request(isolated_source_control_rw_engine, grant.delivery.id)["state"]
        == AgentPushState.AUTHORIZED.value
    )


class BlockingAfterWriteBroker(RestrictedDevAgentPushBroker):
    def __init__(self) -> None:
        super().__init__(
            mode="DEV",
            initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
        )
        self.written = Event()
        self.release = Event()

    def push_and_verify(self, request: BrokerPushRequest) -> BrokerPushObservation:
        observation = super().push_and_verify(request)
        self.written.set()
        assert self.release.wait(timeout=10)
        return observation


def test_push_result_arriving_after_fence_freezes_branch_and_never_confirms(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    broker = BlockingAfterWriteBroker()
    dependencies, _broker, _bindings, _issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine,
        broker=broker,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            execute_agent_push,
            grant.delivery.id,
            raw_grant=RAW_GRANT,
            raw_fencing_token=RAW_FENCE,
            dependencies=dependencies,
        )
        assert broker.written.wait(timeout=10)
        fence_agent_attempt(
            attempt_id=ATTEMPT_ID,
            fenced_generation=3,
            reason_code="ATTEMPT_CANCELLED",
            correlation_id="correlation-fence-race-501",
            dependencies=dependencies,
        )
        broker.release.set()
        delivery = future.result(timeout=10)

    assert delivery.state is AgentPushState.FENCED
    assert delivery.last_error_code == "PUSH_OBSERVED_AFTER_FENCE"
    assert delivery.remote_head_sha == TARGET_COMMIT
    assert (REPOSITORY_ID, BRANCH_NAME) in broker.frozen_branches
    assert _fact_topics(isolated_source_control_rw_engine) == (
        "source-control.agent-push-fenced.v1",
    )


def test_fence_winning_the_final_confirmation_cas_never_emits_success(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    dependencies, broker, _bindings, _issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine
    )

    class FenceBeforeConfirmRepository(SqlAlchemyAgentDeliveryRepository):
        def transition_agent_push(
            self,
            request_id: str,
            *,
            expected_state: str,
            expected_attempts: int | None = None,
            values: Mapping[str, object],
        ) -> Any:
            if (
                expected_state == AgentPushState.IN_FLIGHT.value
                and values.get("state") == AgentPushState.SUCCEEDED.value
            ):
                with isolated_source_control_rw_engine.begin() as concurrent_db:
                    concurrent_repository = SqlAlchemyAgentDeliveryRepository(concurrent_db)
                    concurrent_repository.upsert_attempt_fence(
                        attempt_id=ATTEMPT_ID,
                        fenced_generation=3,
                        reason_code="ATTEMPT_CANCELLED",
                        correlation_id="correlation-final-cas-fence-501",
                        now=NOW,
                        next_revoke_at=NOW,
                    )
                    concurrent_repository.fence_open_agent_pushes(
                        attempt_id=ATTEMPT_ID,
                        fenced_generation=3,
                        reason_code="ATTEMPT_CANCELLED",
                        now=NOW,
                    )
            return super().transition_agent_push(
                request_id,
                expected_state=expected_state,
                expected_attempts=expected_attempts,
                values=values,
            )

    def repository_factory(db: Connection) -> FenceBeforeConfirmRepository:
        return FenceBeforeConfirmRepository(db)

    dependencies = replace(
        dependencies,
        agent_delivery_repository_factory=repository_factory,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    delivery = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    assert delivery.state is AgentPushState.FENCED
    assert delivery.last_error_code == "PUSH_OBSERVED_AFTER_FENCE"
    assert delivery.remote_head_sha == TARGET_COMMIT
    assert (REPOSITORY_ID, BRANCH_NAME) in broker.frozen_branches
    assert _fact_topics(isolated_source_control_rw_engine) == (
        "source-control.agent-push-fenced.v1",
    )


class UnknownThenSuccessfulRevocationBroker(RestrictedDevAgentPushBroker):
    def __init__(self) -> None:
        super().__init__(
            mode="DEV",
            initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
        )
        self.revoke_calls = 0

    def revoke(self, locator: BrokerGrantLocator) -> BrokerRevocationResult:
        self.revoke_calls += 1
        if self.revoke_calls == 1:
            raise AgentPushResultUnknown("revocation outcome unknown")
        return super().revoke(locator)


class HigherFenceDuringRevocationBroker(RestrictedDevAgentPushBroker):
    def __init__(self, engine: Engine) -> None:
        super().__init__(
            mode="DEV",
            initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
        )
        self.engine = engine
        self.injected = False

    def revoke(self, locator: BrokerGrantLocator) -> BrokerRevocationResult:
        if not self.injected:
            self.injected = True
            with self.engine.begin() as db:
                SqlAlchemyAgentDeliveryRepository(db).upsert_attempt_fence(
                    attempt_id=locator.attempt_id,
                    fenced_generation=locator.fenced_generation + 2,
                    reason_code="ATTEMPT_RESTARTED",
                    correlation_id="correlation-higher-fence-601",
                    now=NOW + timedelta(seconds=1),
                    next_revoke_at=NOW + timedelta(seconds=1),
                )
        return super().revoke(locator)


def test_older_revocation_completion_cannot_mark_a_newer_fence_succeeded(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    broker = HigherFenceDuringRevocationBroker(isolated_source_control_rw_engine)
    dependencies, _broker, _bindings, _issuer, _clock, _audit = _dependencies(
        isolated_source_control_rw_engine,
        broker=broker,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    result = fence_agent_attempt(
        attempt_id=ATTEMPT_ID,
        fenced_generation=3,
        reason_code="ATTEMPT_CANCELLED",
        correlation_id="correlation-lower-fence-601",
        dependencies=dependencies,
    )

    with isolated_source_control_rw_engine.connect() as db:
        stored = db.execute(
            text(
                "SELECT fenced_generation, revocation_state "
                "FROM source_control.agent_delivery_fence WHERE attempt_id=:attempt_id"
            ),
            {"attempt_id": ATTEMPT_ID},
        ).one()
    assert stored == (5, "PENDING")
    assert result.revocation_state == "PENDING"
    assert grant.delivery.state is AgentPushState.AUTHORIZED


def test_unknown_revocation_stays_locally_fenced_and_reconciles_without_reopening(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    broker = UnknownThenSuccessfulRevocationBroker()
    dependencies, _broker, _bindings, _issuer, clock, _audit = _dependencies(
        isolated_source_control_rw_engine,
        broker=broker,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )

    first = fence_agent_attempt(
        attempt_id=ATTEMPT_ID,
        fenced_generation=3,
        reason_code="ATTEMPT_CANCELLED",
        correlation_id="correlation-revoke-601",
        dependencies=dependencies,
    )
    clock.value = NOW + timedelta(seconds=16)
    batch = reconcile_agent_revocations(limit=10, dependencies=dependencies)

    assert first.revocation_state == "UNKNOWN"
    assert batch.claimed == 1
    assert batch.succeeded == 1
    assert broker.revoke_calls == 2
    assert (
        _stored_request(isolated_source_control_rw_engine, grant.delivery.id)["state"]
        == AgentPushState.FENCED.value
    )
