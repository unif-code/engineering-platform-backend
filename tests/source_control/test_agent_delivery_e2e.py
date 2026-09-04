from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.source_control import (
    AgentExecutionBindingSnapshot,
    AgentPushBindingRejected,
    AgentPushRequestSpec,
    AgentPushState,
    authorize_agent_push,
    execute_agent_push,
    fence_agent_attempt,
    reconcile_agent_pushes,
)
from control_plane.app.modules.source_control.adapters import (
    DevBrokerBehavior,
    RestrictedDevAgentPushBroker,
)
from control_plane.app.modules.source_control.ports import BrokerPushObservation, BrokerPushRequest
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_agent_delivery_adapters import (
    BRANCH_NAME,
    EXPECTED_HEAD,
    NOW,
    REPOSITORY_ID,
    TARGET_COMMIT,
    WORKSPACE_ID,
    _seed_branch,
)
from tests.source_control.test_agent_delivery_api import CapabilityGuard, _client
from tests.source_control.test_agent_delivery_commands import (
    RAW_FENCE,
    RAW_GRANT,
    FixedExecutionBindings,
    _dependencies,
    _spec,
)


def _decision_count(engine: Engine) -> int:
    with engine.connect() as db:
        return int(db.execute(text("SELECT count(*) FROM requirement.decision")).scalar_one())


def _query_client(
    engine: Engine,
    *,
    dependencies: object,
) -> TestClient:
    return _client(
        engine,
        dependencies=dependencies,
        guard=CapabilityGuard({("requirement.read", WORKSPACE_ID)}),
    )


class MutableGenerationBindings(FixedExecutionBindings):
    attempt_generation: int | None = None

    def validate(
        self,
        spec: AgentPushRequestSpec,
        *,
        raw_fencing_token: str,
    ) -> AgentExecutionBindingSnapshot:
        snapshot = super().validate(spec, raw_fencing_token=raw_fencing_token)
        if self.attempt_generation is None:
            return snapshot
        return snapshot.model_copy(update={"attempt_generation": self.attempt_generation})


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


def test_authorize_push_confirm_fact_and_human_query_without_decision_mutation(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    runtime_engine = isolated_source_control_database.runtime
    owner_engine = isolated_source_control_database.owner
    _seed_branch(runtime_engine)
    dependencies, broker, _bindings, _issuer, _clock, _audit = _dependencies(runtime_engine)
    dependencies = replace(
        dependencies,
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    decisions_before = _decision_count(owner_engine)

    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    delivered = execute_agent_push(
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
    response = _query_client(runtime_engine, dependencies=dependencies).get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{grant.delivery.id}"
    )

    assert delivered.state is replay.state is AgentPushState.SUCCEEDED
    assert broker.push_count == broker.write_count == 1
    assert response.status_code == 200
    assert response.json()["state"] == "SUCCEEDED"
    assert response.json()["remoteHeadSha"] == TARGET_COMMIT
    with owner_engine.connect() as db:
        fact = db.execute(
            text(
                "SELECT topic, payload FROM source_control.agent_delivery_fact "
                "WHERE push_request_id=:request_id"
            ),
            {"request_id": grant.delivery.id},
        ).one()
        audit_actions = tuple(
            db.execute(
                text(
                    "SELECT action FROM audit.audit_event "
                    "WHERE target_id=:request_id ORDER BY occurred_at, id"
                ),
                {"request_id": grant.delivery.id},
            ).scalars()
        )
    assert fact.topic == "source-control.agent-push-confirmed.v1"
    assert fact.payload["executorType"] == "AGENT"
    assert fact.payload["targetCommitSha"] == TARGET_COMMIT
    assert audit_actions == (
        "source_control.agent_push_authorized",
        "source_control.agent_push_consumed",
        "source_control.agent_push_confirmed",
    )
    assert _decision_count(owner_engine) == decisions_before


def test_unknown_after_write_reconciles_by_observation_and_never_repeats_push(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    runtime_engine = isolated_source_control_database.runtime
    _seed_branch(runtime_engine)
    broker = RestrictedDevAgentPushBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
    )
    dependencies, _broker, _bindings, _issuer, clock, _audit = _dependencies(
        runtime_engine,
        broker=broker,
    )
    dependencies = replace(
        dependencies,
        audit=SqlAlchemyTransactionalAuditAppender(),
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
    reconciled = reconcile_agent_pushes(limit=10, dependencies=dependencies)
    replay = execute_agent_push(
        grant.delivery.id,
        raw_grant=RAW_GRANT,
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    response = _query_client(runtime_engine, dependencies=dependencies).get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{grant.delivery.id}"
    )

    assert unknown.state is AgentPushState.UNKNOWN
    assert reconciled.deliveries[0].state is AgentPushState.SUCCEEDED
    assert replay.state is AgentPushState.SUCCEEDED
    assert broker.push_count == broker.write_count == broker.observe_count == 1
    assert response.status_code == 200
    assert response.json()["state"] == "SUCCEEDED"


def test_stale_execution_generation_is_rejected_before_any_external_write(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    runtime_engine = isolated_source_control_database.runtime
    _seed_branch(runtime_engine)
    bindings = MutableGenerationBindings()
    dependencies, broker, _bindings, _issuer, _clock, _audit = _dependencies(
        runtime_engine,
        bindings=bindings,
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    bindings.attempt_generation = 2

    with pytest.raises(AgentPushBindingRejected):
        execute_agent_push(
            grant.delivery.id,
            raw_grant=RAW_GRANT,
            raw_fencing_token=RAW_FENCE,
            dependencies=dependencies,
        )
    response = _query_client(runtime_engine, dependencies=dependencies).get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{grant.delivery.id}"
    )

    assert broker.push_count == broker.write_count == 0
    assert response.status_code == 200
    assert response.json()["state"] == "AUTHORIZED"


def test_push_observed_after_fence_is_frozen_and_visible_as_fenced_not_success(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    runtime_engine = isolated_source_control_database.runtime
    owner_engine = isolated_source_control_database.owner
    _seed_branch(runtime_engine)
    broker = BlockingAfterWriteBroker()
    dependencies, _broker, _bindings, _issuer, _clock, _audit = _dependencies(
        runtime_engine,
        broker=broker,
    )
    dependencies = replace(
        dependencies,
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    grant = authorize_agent_push(
        _spec(),
        raw_fencing_token=RAW_FENCE,
        dependencies=dependencies,
    )
    decisions_before = _decision_count(owner_engine)

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
            attempt_id=grant.delivery.attempt_id,
            fenced_generation=grant.delivery.attempt_generation,
            reason_code="ATTEMPT_CANCELLED",
            correlation_id="correlation-e2e-fence-701",
            dependencies=dependencies,
        )
        broker.release.set()
        fenced = future.result(timeout=10)
    response = _query_client(runtime_engine, dependencies=dependencies).get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/agent-deliveries/{grant.delivery.id}"
    )

    assert fenced.state is AgentPushState.FENCED
    assert response.status_code == 200
    assert response.json()["state"] == "FENCED"
    assert response.json()["lastErrorCode"] == "PUSH_OBSERVED_AFTER_FENCE"
    assert (REPOSITORY_ID, BRANCH_NAME) in broker.frozen_branches
    with owner_engine.connect() as db:
        topics = tuple(
            db.execute(
                text(
                    "SELECT topic FROM source_control.agent_delivery_fact "
                    "WHERE push_request_id=:request_id"
                ),
                {"request_id": grant.delivery.id},
            ).scalars()
        )
    assert topics == ("source-control.agent-push-fenced.v1",)
    assert _decision_count(owner_engine) == decisions_before
