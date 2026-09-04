from dataclasses import asdict, fields
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from control_plane.app.modules.source_control.adapters import (
    DevBrokerBehavior,
    RestrictedDevAgentPushBroker,
    SecureAgentPushGrantIssuer,
    SqlAlchemyAgentDeliveryRepository,
)
from control_plane.app.modules.source_control.domain import digest_agent_push_grant
from control_plane.app.modules.source_control.ports import (
    AgentDeliveryDependencyUnavailable,
    AgentPushDenied,
    AgentPushResultUnknown,
    BrokerGrantLocator,
    BrokerPushLocator,
    BrokerPushRequest,
)
from tests.source_control.conftest import IsolatedSourceControlDatabase

NOW = datetime(2026, 8, 31, 3, 0, tzinfo=UTC)
REPOSITORY_ID = "10000000-0000-0000-0000-000000000301"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000301"
REQUIREMENT_ID = "40000000-0000-0000-0000-000000000301"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000301"
BRANCH_BINDING_ID = "70000000-0000-0000-0000-000000000301"
BRANCH_NAME = "feat/wi-301-source-control"
ATTEMPT_ID = "92000000-0000-0000-0000-000000000301"
EXPECTED_HEAD = "a" * 40
TARGET_COMMIT = "b" * 40
CONTENT_DIGEST = f"sha256:{'c' * 64}"
BINDING_DIGEST = f"sha256:{'d' * 64}"
GRANT_DIGEST = f"sha256:{'e' * 64}"


def _seed_branch(engine: Engine) -> None:
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.workspace_repository "
                "(id, workspace_id, provider, project_id, project_path, default_branch, "
                "connection_ref, credential_secret_ref, status, revision) VALUES "
                "(:repository_id, :workspace_id, 'GITLAB', '301', 'platform/backend', "
                "'main', 'gitlab-dev', 'secret-ref:test-only', 'AUTHORIZED', 1)"
            ),
            {"repository_id": REPOSITORY_ID, "workspace_id": WORKSPACE_ID},
        )
        db.execute(
            text(
                "INSERT INTO source_control.source_control_effect "
                "(id, effect_key, operation, subject_key, payload, work_item_id, "
                "requirement_id, repository_id, work_item_number, branch_name, "
                "base_commit_sha, request_fingerprint, attempts, state, "
                "requirement_callback_state, completed_at) VALUES "
                "('60000000-0000-0000-0000-000000000301', 'create:work-item-301', "
                "'CREATE_TASK_BRANCH', :subject_key, '{}'::jsonb, :work_item_id, "
                ":requirement_id, :repository_id, 301, :branch_name, :expected_head, "
                "'sha256:branch', 0, 'SUCCEEDED', 'ACKED', now())"
            ),
            {
                "subject_key": f"work-item:{WORK_ITEM_ID}",
                "work_item_id": WORK_ITEM_ID,
                "requirement_id": REQUIREMENT_ID,
                "repository_id": REPOSITORY_ID,
                "branch_name": BRANCH_NAME,
                "expected_head": EXPECTED_HEAD,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.repository_branch_binding "
                "(id, work_item_id, requirement_id, workspace_id, repository_id, "
                "work_item_number, base_commit_sha, branch_name, effect_id) VALUES "
                "(:binding_id, :work_item_id, :requirement_id, :workspace_id, "
                ":repository_id, 301, :expected_head, :branch_name, "
                "'60000000-0000-0000-0000-000000000301')"
            ),
            {
                "binding_id": BRANCH_BINDING_ID,
                "work_item_id": WORK_ITEM_ID,
                "requirement_id": REQUIREMENT_ID,
                "workspace_id": WORKSPACE_ID,
                "repository_id": REPOSITORY_ID,
                "expected_head": EXPECTED_HEAD,
                "branch_name": BRANCH_NAME,
            },
        )


def _request_values(
    request_id: str,
    *,
    attempt_id: str = ATTEMPT_ID,
    state: str = "AUTHORIZED",
    target_commit: str = TARGET_COMMIT,
) -> dict[str, object]:
    issued_at = NOW - timedelta(seconds=30)
    values: dict[str, object] = {
        "id": request_id,
        "idempotency_key": f"agent-push:{request_id}",
        "request_fingerprint": f"sha256:{'1' * 64}",
        "attempt_id": attempt_id,
        "attempt_generation": 3,
        "execution_binding_digest": BINDING_DIGEST,
        "requirement_id": REQUIREMENT_ID,
        "work_item_id": WORK_ITEM_ID,
        "workspace_id": WORKSPACE_ID,
        "repository_id": REPOSITORY_ID,
        "branch_binding_id": BRANCH_BINDING_ID,
        "branch_name": BRANCH_NAME,
        "expected_remote_head_sha": EXPECTED_HEAD,
        "target_commit_sha": target_commit,
        "content_digest": CONTENT_DIGEST,
        "artifact_refs": ("artifact://patch/301",),
        "grant_digest": GRANT_DIGEST,
        "correlation_id": f"correlation:{request_id}",
        "state": state,
        "attempts": 0,
        "issued_at": issued_at,
        "expires_at": NOW + timedelta(seconds=30),
        "next_reconcile_at": None,
        "consumed_at": None,
        "observed_at": None,
        "completed_at": None,
        "remote_head_sha": None,
        "last_error_code": None,
        "created_at": issued_at,
        "updated_at": issued_at,
    }
    if state == "UNKNOWN":
        values.update(
            attempts=1,
            consumed_at=issued_at + timedelta(seconds=1),
            next_reconcile_at=NOW,
            last_error_code="RESULT_UNKNOWN",
            updated_at=NOW,
        )
    return values


def _locator(request_id: str) -> BrokerPushLocator:
    return BrokerPushLocator(
        request_id=request_id,
        attempt_id=ATTEMPT_ID,
        attempt_generation=3,
        repository_id=REPOSITORY_ID,
        branch_name=BRANCH_NAME,
        expected_remote_head_sha=EXPECTED_HEAD,
        target_commit_sha=TARGET_COMMIT,
        content_digest=CONTENT_DIGEST,
    )


def _push_request(request_id: str) -> BrokerPushRequest:
    return BrokerPushRequest(
        **asdict(_locator(request_id)),
        execution_binding_digest=BINDING_DIGEST,
    )


def test_sql_repository_consumes_one_grant_once_with_compare_and_set(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    request_id = "91000000-0000-0000-0000-000000000311"
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemyAgentDeliveryRepository(db)
        repository.insert_agent_push(**_request_values(request_id))

    with isolated_source_control_rw_engine.begin() as db:
        first = SqlAlchemyAgentDeliveryRepository(db).consume_agent_push(
            request_id=request_id,
            grant_digest=GRANT_DIGEST,
            now=NOW,
            next_reconcile_at=NOW + timedelta(seconds=15),
        )
    with isolated_source_control_rw_engine.begin() as db:
        second = SqlAlchemyAgentDeliveryRepository(db).consume_agent_push(
            request_id=request_id,
            grant_digest=GRANT_DIGEST,
            now=NOW,
            next_reconcile_at=NOW + timedelta(seconds=15),
        )

    assert first is not None
    assert first["state"] == "IN_FLIGHT"
    assert first["attempts"] == 1
    assert second is None


def test_sql_repository_reconciliation_claims_are_disjoint(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    request_ids = (
        "91000000-0000-0000-0000-000000000321",
        "91000000-0000-0000-0000-000000000322",
    )
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemyAgentDeliveryRepository(db)
        repository.insert_agent_push(**_request_values(request_ids[0], state="UNKNOWN"))
        repository.insert_agent_push(
            **_request_values(
                request_ids[1],
                attempt_id="92000000-0000-0000-0000-000000000322",
                state="UNKNOWN",
                target_commit="c" * 40,
            )
        )

    first_connection = isolated_source_control_rw_engine.connect()
    second_connection = isolated_source_control_rw_engine.connect()
    first_transaction = first_connection.begin()
    second_transaction = second_connection.begin()
    try:
        first = SqlAlchemyAgentDeliveryRepository(first_connection).claim_reconcilable(
            limit=1,
            now=NOW,
            lease_until=NOW + timedelta(seconds=15),
        )
        second = SqlAlchemyAgentDeliveryRepository(second_connection).claim_reconcilable(
            limit=1,
            now=NOW,
            lease_until=NOW + timedelta(seconds=15),
        )
        assert {str(first[0]["id"]), str(second[0]["id"])} == set(request_ids)
        assert first[0]["id"] != second[0]["id"]
    finally:
        first_transaction.rollback()
        second_transaction.rollback()
        first_connection.close()
        second_connection.close()


def test_sql_repository_fence_is_monotonic_and_fences_open_requests(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_branch(isolated_source_control_rw_engine)
    request_id = "91000000-0000-0000-0000-000000000331"
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemyAgentDeliveryRepository(db)
        repository.insert_agent_push(**_request_values(request_id))
        first = repository.upsert_attempt_fence(
            attempt_id=ATTEMPT_ID,
            fenced_generation=3,
            reason_code="ATTEMPT_CANCELLED",
            correlation_id="correlation-fence-331",
            now=NOW,
            next_revoke_at=NOW,
        )
        stale = repository.upsert_attempt_fence(
            attempt_id=ATTEMPT_ID,
            fenced_generation=2,
            reason_code="STALE_FENCE",
            correlation_id="correlation-stale-331",
            now=NOW + timedelta(seconds=1),
            next_revoke_at=NOW + timedelta(seconds=1),
        )
        fenced = repository.fence_open_agent_pushes(
            attempt_id=ATTEMPT_ID,
            fenced_generation=3,
            reason_code="ATTEMPT_CANCELLED",
            now=NOW,
        )

    assert first["fenced_generation"] == 3
    assert stale["fenced_generation"] == 3
    assert stale["reason_code"] == "ATTEMPT_CANCELLED"
    assert [str(row["id"]) for row in fenced] == [request_id]
    assert fenced[0]["state"] == "FENCED"


def test_agent_delivery_fact_is_append_only_for_runtime_role(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_branch(isolated_source_control_database.runtime)
    request_id = "91000000-0000-0000-0000-000000000341"
    fact_id = "93000000-0000-0000-0000-000000000341"
    with isolated_source_control_database.runtime.begin() as db:
        repository = SqlAlchemyAgentDeliveryRepository(db)
        repository.insert_agent_push(**_request_values(request_id))
        repository.insert_fact(
            id=fact_id,
            push_request_id=request_id,
            topic="source-control.agent-push-confirmed.v1",
            payload={"executorType": "AGENT", "attemptId": ATTEMPT_ID},
            correlation_id="correlation-fact-341",
            occurred_at=NOW,
        )

    with pytest.raises(DBAPIError):
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                text(
                    "UPDATE source_control.agent_delivery_fact "
                    "SET topic='source-control.agent-push-fenced.v1' WHERE id=:id"
                ),
                {"id": fact_id},
            )
    with pytest.raises(DBAPIError):
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                text("DELETE FROM source_control.agent_delivery_fact WHERE id=:id"),
                {"id": fact_id},
            )


def test_secure_grant_issuer_returns_raw_once_without_retaining_it() -> None:
    issuer = SecureAgentPushGrantIssuer()

    issued = issuer.issue()

    assert issued.digest == digest_agent_push_grant(issued.raw)
    assert issued.raw not in repr(issued)
    assert issued.raw not in repr(issuer)
    assert not hasattr(issuer, "__dict__")


def test_restricted_dev_broker_refuses_non_dev_mode() -> None:
    with pytest.raises(AgentDeliveryDependencyUnavailable):
        RestrictedDevAgentPushBroker(mode="PROD")


def test_restricted_dev_broker_models_success_unknown_denial_revoke_and_freeze() -> None:
    success_id = "91000000-0000-0000-0000-000000000351"
    unknown_id = "91000000-0000-0000-0000-000000000352"
    denied_id = "91000000-0000-0000-0000-000000000353"
    broker = RestrictedDevAgentPushBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
        behaviors={
            unknown_id: DevBrokerBehavior.UNKNOWN_AFTER_WRITE,
            denied_id: DevBrokerBehavior.DENIED,
        },
    )

    success = broker.push_and_verify(_push_request(success_id))
    assert success.remote_head_sha == TARGET_COMMIT
    assert broker.write_count == 1

    broker.seed_remote_head(REPOSITORY_ID, BRANCH_NAME, EXPECTED_HEAD)
    with pytest.raises(AgentPushResultUnknown):
        broker.push_and_verify(_push_request(unknown_id))
    assert broker.observe(_locator(unknown_id)).remote_head_sha == TARGET_COMMIT
    assert broker.write_count == 2

    broker.seed_remote_head(REPOSITORY_ID, BRANCH_NAME, EXPECTED_HEAD)
    with pytest.raises(AgentPushDenied):
        broker.push_and_verify(_push_request(denied_id))
    assert broker.observe(_locator(denied_id)).remote_head_sha == EXPECTED_HEAD
    assert broker.write_count == 2

    revocation = broker.revoke(
        BrokerGrantLocator(
            attempt_id=ATTEMPT_ID,
            fenced_generation=3,
            repository_id=REPOSITORY_ID,
            branch_name=BRANCH_NAME,
        )
    )
    freeze = broker.freeze_branch(_locator(unknown_id))
    assert revocation.revoked is True
    assert freeze.frozen is True
    assert (REPOSITORY_ID, BRANCH_NAME) in broker.frozen_branches


def test_restricted_dev_broker_revokes_every_generation_covered_by_the_fence() -> None:
    request_id = "91000000-0000-0000-0000-000000000354"
    broker = RestrictedDevAgentPushBroker(
        mode="DEV",
        initial_heads={(REPOSITORY_ID, BRANCH_NAME): EXPECTED_HEAD},
    )
    broker.revoke(
        BrokerGrantLocator(
            attempt_id=ATTEMPT_ID,
            fenced_generation=5,
            repository_id=REPOSITORY_ID,
            branch_name=BRANCH_NAME,
        )
    )

    with pytest.raises(AgentPushDenied):
        broker.push_and_verify(_push_request(request_id))

    assert broker.write_count == 0
    assert broker.observe(_locator(request_id)).remote_head_sha == EXPECTED_HEAD


def test_broker_port_models_have_no_credential_or_execution_surface() -> None:
    models = (BrokerPushRequest, BrokerPushLocator, BrokerGrantLocator)
    forbidden = ("credential", "secret", "token", "environment", "path", "command")

    for model in models:
        names = {field.name.lower() for field in fields(model)}
        assert all(fragment not in name for fragment in forbidden for name in names)
