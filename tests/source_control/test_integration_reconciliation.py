from datetime import timedelta
from typing import Literal

import pytest
from sqlalchemy import Engine

from control_plane.app.modules.source_control import (
    EffectState,
    ReconcileDueIntegrationEffectsResult,
    RequirementCallbackUnavailable,
    process_integration_mr_request,
    reconcile_due_integration_effects,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlIntegrationRepository,
)
from control_plane.app.modules.source_control.ports import (
    GitLabBranchNotFound,
    GitLabMergeRequestBlocked,
    GitLabProviderUnavailable,
    GitLabResultUnknown,
)
from tests.source_control.test_integration_merge_saga import (
    MERGE_COMMIT_SHA,
    _merged_snapshot,
    _opened_snapshot,
    _seed_merge_request,
    _seed_planned_merge_effect,
)
from tests.source_control.test_integration_merge_saga import (
    NOW as MERGE_NOW,
)
from tests.source_control.test_integration_merge_saga import (
    _dependencies as _merge_dependencies,
)
from tests.source_control.test_integration_mr_saga import (
    HEAD_SHA as MERGE_HEAD_SHA,
)
from tests.source_control.test_integration_mr_saga import (
    MESSAGE_ID,
    NOW,
    WORK_ITEM_ID,
    _dependencies,
    _mr_snapshot,
    _seed_integration_effect,
    _seed_source_control,
)


def test_reconciliation_returns_empty_result_when_no_integration_effect_is_due(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, _requirement, _gitlab = _dependencies(isolated_source_control_rw_engine)

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result == ReconcileDueIntegrationEffectsResult(effects=())


def test_dual_reconciliation_claims_skip_the_locked_effect(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s "
            "WHERE operation='CREATE_INTEGRATION_MR'",
            (NOW,),
        )
    first_connection = isolated_source_control_rw_engine.connect()
    second_connection = isolated_source_control_rw_engine.connect()
    first_transaction = first_connection.begin()
    second_transaction = second_connection.begin()
    try:
        first = SqlAlchemySourceControlIntegrationRepository(first_connection).claim_effects(
            limit=1,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )
        second = SqlAlchemySourceControlIntegrationRepository(second_connection).claim_effects(
            limit=1,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )
        assert len(first) == 1
        assert second == []
    finally:
        second_transaction.rollback()
        first_transaction.rollback()
        second_connection.close()
        first_connection.close()


def test_reconciler_claim_fences_an_older_create_saga_fact_commit(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    dependencies, requirement, gitlab = _dependencies(isolated_source_control_rw_engine)

    def claim_expired_saga_effect() -> None:
        gitlab.before_readback = None
        with isolated_source_control_rw_engine.begin() as db:
            claims = SqlAlchemySourceControlIntegrationRepository(db).claim_effects(
                limit=1,
                now=NOW + timedelta(minutes=2),
                lease_until=NOW + timedelta(minutes=4),
            )
        assert len(claims) == 1
        assert claims[0]["state"] == EffectState.RECONCILIATION.value
        assert claims[0]["attempts"] == 2

    gitlab.before_readback = claim_expired_saga_effect

    with pytest.raises(RequirementCallbackUnavailable, match="effect lease was lost"):
        process_integration_mr_request(
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )

    with isolated_source_control_rw_engine.connect() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        effect = repository.effect_by_operation_subject(
            "CREATE_INTEGRATION_MR",
            f"work-item:{WORK_ITEM_ID}",
        )
        binding = repository.merge_request_binding_by_work_item(WORK_ITEM_ID)
        observations = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.merge_request_observation"
        ).scalar_one()
    assert effect["state"] == EffectState.RECONCILIATION.value
    assert effect["attempts"] == 2
    assert binding is None
    assert observations == 0
    assert requirement.ready == []
    assert requirement.blocked == []


def test_unknown_create_reconciles_unique_existing_mr_without_second_post(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, requirement, gitlab = _dependencies(isolated_source_control_rw_engine)
    gitlab.candidates = [_mr_snapshot()]

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        binding = SqlAlchemySourceControlIntegrationRepository(
            db
        ).merge_request_binding_by_work_item(result.effects[0].work_item_id)
    assert result.effects[0].state is EffectState.SUCCEEDED
    assert result.effects[0].attempts == 1
    assert "create_mr" not in gitlab.calls
    assert binding is not None
    assert binding["merge_request_iid"] == 17
    assert requirement.ready_attempts == 1


def test_unknown_create_blocks_multiple_candidates_without_provider_write(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, requirement, gitlab = _dependencies(isolated_source_control_rw_engine)
    gitlab.candidates = [_mr_snapshot(iid=17), _mr_snapshot(iid=18)]

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        binding = SqlAlchemySourceControlIntegrationRepository(
            db
        ).merge_request_binding_by_work_item(result.effects[0].work_item_id)
    assert result.effects[0].state is EffectState.BLOCKED
    assert result.effects[0].last_error_code == "MR_CONFLICT"
    assert "create_mr" not in gitlab.calls
    assert binding is None
    assert requirement.blocked[-1].reason_code == "MR_CONFLICT"


def test_unknown_create_retries_post_with_same_effect_after_no_candidate(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, _requirement, gitlab = _dependencies(isolated_source_control_rw_engine)
    gitlab.expected_effect_state = EffectState.RECONCILIATION

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        effects = db.exec_driver_sql(
            "SELECT id, attempts FROM source_control.source_control_effect "
            "WHERE operation='CREATE_INTEGRATION_MR'"
        ).all()
    assert result.effects[0].state is EffectState.SUCCEEDED
    assert gitlab.calls.count("create_mr") == 1
    assert [(str(row[0]), row[1]) for row in effects] == [
        ("80000000-0000-0000-0000-000000000701", 1)
    ]


def test_create_callback_replay_performs_no_provider_write(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, requirement, gitlab = _dependencies(isolated_source_control_rw_engine)
    gitlab.candidates = [_mr_snapshot()]
    requirement.fail_ready = True

    first = reconcile_due_integration_effects(limit=10, dependencies=dependencies)
    gitlab.calls.clear()
    requirement.fail_ready = False
    second = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert first.effects[0].state is EffectState.SUCCEEDED
    assert first.effects[0].callback_state.value == "FAILED"
    assert second.effects[0].state is EffectState.SUCCEEDED
    assert second.effects[0].callback_state.value == "ACKED"
    assert gitlab.calls == []
    assert requirement.ready_attempts == 2


def test_unknown_create_provider_failure_returns_to_unknown_with_backoff(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, requirement, gitlab = _dependencies(isolated_source_control_rw_engine)
    gitlab.list_error = GitLabProviderUnavailable("unavailable")

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.UNKNOWN
    assert result.effects[0].next_reconcile_at == NOW + timedelta(minutes=1)
    assert requirement.ready == []
    assert requirement.blocked == []


@pytest.mark.parametrize(
    ("provider_state", "expected_reason", "callback_kind"),
    [
        ("closed", "MR_CLOSED", "blocked"),
        ("merged", "EXTERNAL_MERGE_DRIFT", "external_drift"),
    ],
)
def test_unknown_create_persists_terminal_provider_fact_before_blocking(
    isolated_source_control_rw_engine: Engine,
    provider_state: Literal["closed", "merged"],
    expected_reason: str,
    callback_kind: str,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, requirement, gitlab = _dependencies(isolated_source_control_rw_engine)
    snapshot = _mr_snapshot(state=provider_state)
    if provider_state == "merged":
        snapshot = snapshot.model_copy(
            update={
                "merge_commit_sha": "d" * 40,
                "merge_user_id": "provider-user-17",
                "merged_at": NOW,
            }
        )
    gitlab.candidates = [snapshot]
    gitlab.readback = snapshot

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        observation = db.exec_driver_sql(
            "SELECT state, merge_commit_sha FROM source_control.merge_request_observation"
        ).one()
    assert result.effects[0].state is EffectState.BLOCKED
    assert result.effects[0].last_error_code == expected_reason
    assert observation[0] == provider_state.upper()
    if callback_kind == "blocked":
        assert requirement.blocked[-1].reason_code == expected_reason
        assert requirement.external_drift == []
    else:
        assert requirement.external_drift[-1].binding_id
        assert requirement.blocked == []


@pytest.mark.parametrize(
    "candidate",
    [
        _mr_snapshot(source_branch="feat/other"),
        _mr_snapshot(target_branch="main"),
    ],
)
def test_unknown_create_blocks_incompatible_candidate(
    isolated_source_control_rw_engine: Engine,
    candidate: object,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, _requirement, gitlab = _dependencies(isolated_source_control_rw_engine)
    gitlab.candidates = [candidate]  # type: ignore[list-item]

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.BLOCKED
    assert result.effects[0].last_error_code == "MR_CONFLICT"
    assert "create_mr" not in gitlab.calls


def test_unknown_create_locked_snapshot_remains_unknown(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, _requirement, gitlab = _dependencies(isolated_source_control_rw_engine)
    locked = _mr_snapshot(state="locked")
    gitlab.candidates = [locked]
    gitlab.readback = locked

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.UNKNOWN
    with isolated_source_control_rw_engine.connect() as db:
        binding_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.merge_request_binding"
        ).scalar_one()
    assert binding_count == 0


def test_create_reconciler_renews_ownership_before_retry_post(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, requirement, gitlab = _dependencies(isolated_source_control_rw_engine)

    def steal_lease() -> None:
        gitlab.after_list = None
        with isolated_source_control_rw_engine.begin() as db:
            claims = SqlAlchemySourceControlIntegrationRepository(db).claim_effects(
                limit=1,
                now=NOW + timedelta(minutes=2),
                lease_until=NOW + timedelta(minutes=4),
            )
        assert len(claims) == 1

    gitlab.after_list = steal_lease
    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.RECONCILIATION
    assert result.effects[0].attempts == 2
    assert "create_mr" not in gitlab.calls
    assert requirement.ready == []


def test_stale_create_worker_rolls_back_facts_after_provider_readback(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    dependencies, requirement, gitlab = _dependencies(isolated_source_control_rw_engine)
    gitlab.candidates = [_mr_snapshot()]

    def steal_lease() -> None:
        gitlab.before_readback = None
        with isolated_source_control_rw_engine.begin() as db:
            claims = SqlAlchemySourceControlIntegrationRepository(db).claim_effects(
                limit=1,
                now=NOW + timedelta(minutes=2),
                lease_until=NOW + timedelta(minutes=4),
            )
        assert len(claims) == 1

    gitlab.before_readback = steal_lease
    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        binding_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.merge_request_binding"
        ).scalar_one()
        observation_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.merge_request_observation"
        ).scalar_one()
    assert result.effects[0].state is EffectState.RECONCILIATION
    assert result.effects[0].attempts == 2
    assert binding_count == 0
    assert observation_count == 0
    assert requirement.ready == []


def test_unknown_merge_reconciles_only_exact_merged_head(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.preflight_snapshot = _merged_snapshot()

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        latest = db.exec_driver_sql(
            "SELECT state, head_sha, merge_commit_sha FROM "
            "source_control.merge_request_observation "
            "ORDER BY observed_at DESC, id DESC LIMIT 1"
        ).one()
    assert result.effects[0].state is EffectState.SUCCEEDED
    assert latest[0] == "MERGED"
    assert latest[1] == MERGE_HEAD_SHA
    assert latest[2] == MERGE_COMMIT_SHA
    assert gitlab.merge_calls == []
    assert requirement.merged_attempts == 1


def test_unknown_merge_retries_put_with_same_exact_head(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.expected_effect_state = EffectState.RECONCILIATION

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.SUCCEEDED
    assert gitlab.merge_calls == [(17, MERGE_HEAD_SHA)]
    assert requirement.merged_attempts == 1


def test_unknown_merge_preserves_merged_fact_when_source_is_missing(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab._merged = True
    gitlab.source_after_merge_error = GitLabBranchNotFound("missing")

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        latest = db.exec_driver_sql(
            "SELECT state, merge_commit_sha FROM "
            "source_control.merge_request_observation "
            "ORDER BY observed_at DESC, id DESC LIMIT 1"
        ).one()
    assert result.effects[0].state is EffectState.BLOCKED
    assert result.effects[0].last_error_code == ("SOURCE_BRANCH_MISSING_AFTER_INTEGRATION")
    assert latest == ("MERGED", MERGE_COMMIT_SHA)
    assert requirement.blocked[-1].reason_code == ("SOURCE_BRANCH_MISSING_AFTER_INTEGRATION")


@pytest.mark.parametrize(
    ("snapshot", "expected_reason"),
    [
        (_opened_snapshot().model_copy(update={"state": "closed"}), "MR_CLOSED"),
        (
            _opened_snapshot().model_copy(update={"has_conflicts": True}),
            "MERGE_CONFLICT",
        ),
        (
            _opened_snapshot().model_copy(update={"head_pipeline_status": "failed"}),
            "MR_CHECKS_BLOCKED",
        ),
    ],
)
def test_unknown_merge_stably_blocks_closed_checks_or_conflict(
    isolated_source_control_rw_engine: Engine,
    snapshot: object,
    expected_reason: str,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.preflight_snapshot = snapshot  # type: ignore[assignment]

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.BLOCKED
    assert result.effects[0].last_error_code == expected_reason
    assert gitlab.merge_calls == []
    assert requirement.blocked[-1].reason_code == expected_reason


def test_unknown_merge_blocks_head_drift_without_put(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.source_head = "f" * 40

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.BLOCKED
    assert result.effects[0].last_error_code == "HEAD_SHA_CHANGED"
    assert gitlab.merge_calls == []


@pytest.mark.parametrize("failure_stage", ["get", "put"])
def test_unknown_merge_provider_uncertainty_returns_to_unknown(
    isolated_source_control_rw_engine: Engine,
    failure_stage: str,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.expected_effect_state = EffectState.RECONCILIATION
    if failure_stage == "get":
        gitlab.profile_error = GitLabProviderUnavailable("unavailable")
    else:
        gitlab.merge_error = GitLabResultUnknown("timeout")

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.UNKNOWN
    assert result.effects[0].next_reconcile_at == MERGE_NOW + timedelta(minutes=2)
    assert requirement.merged == []
    assert requirement.blocked == []


def test_incomplete_merged_snapshot_remains_unknown_without_constraint_error(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.preflight_snapshot = _opened_snapshot().model_copy(update={"state": "merged"})

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.UNKNOWN
    with isolated_source_control_rw_engine.connect() as db:
        observation_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.merge_request_observation"
        ).scalar_one()
    assert observation_count == 1


@pytest.mark.parametrize(
    ("merge_error", "expected_reason"),
    [
        (GitLabMergeRequestBlocked("blocked"), "MERGE_CONFLICT"),
    ],
)
def test_unknown_merge_stable_put_rejection_blocks_same_effect(
    isolated_source_control_rw_engine: Engine,
    merge_error: Exception,
    expected_reason: str,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.expected_effect_state = EffectState.RECONCILIATION
    gitlab.merge_error = merge_error

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.BLOCKED
    assert result.effects[0].last_error_code == expected_reason


def test_unknown_merge_post_put_missing_source_preserves_fact_and_blocks(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.expected_effect_state = EffectState.RECONCILIATION
    gitlab.source_after_merge_error = GitLabBranchNotFound("missing")

    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        latest_state = db.exec_driver_sql(
            "SELECT state FROM source_control.merge_request_observation "
            "ORDER BY observed_at DESC, id DESC LIMIT 1"
        ).scalar_one()
    assert result.effects[0].state is EffectState.BLOCKED
    assert result.effects[0].last_error_code == ("SOURCE_BRANCH_MISSING_AFTER_INTEGRATION")
    assert latest_state == "MERGED"


def test_merge_callback_replay_performs_no_provider_write(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.preflight_snapshot = _merged_snapshot()
    requirement.fail_merged = True

    first = reconcile_due_integration_effects(limit=10, dependencies=dependencies)
    gitlab.calls.clear()
    requirement.fail_merged = False
    second = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert first.effects[0].callback_state.value == "FAILED"
    assert second.effects[0].callback_state.value == "ACKED"
    assert gitlab.calls == []
    assert requirement.merged_attempts == 2


def test_merge_reconciler_renews_ownership_before_put(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )

    def steal_lease() -> None:
        with isolated_source_control_rw_engine.begin() as db:
            claims = SqlAlchemySourceControlIntegrationRepository(db).claim_effects(
                limit=1,
                now=MERGE_NOW + timedelta(minutes=2),
                lease_until=MERGE_NOW + timedelta(minutes=4),
            )
        assert len(claims) == 1

    gitlab.after_preflight_read = steal_lease
    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.RECONCILIATION
    assert result.effects[0].attempts == 3
    assert gitlab.merge_calls == []
    assert requirement.merged == []


def test_stale_merge_worker_rolls_back_observation_after_readback(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head=MERGE_HEAD_SHA,
        state=EffectState.UNKNOWN,
    )
    dependencies, requirement, _eligibility, gitlab = _merge_dependencies(
        isolated_source_control_rw_engine
    )
    gitlab.expected_effect_state = EffectState.RECONCILIATION

    def steal_lease() -> None:
        with isolated_source_control_rw_engine.begin() as db:
            claims = SqlAlchemySourceControlIntegrationRepository(db).claim_effects(
                limit=1,
                now=MERGE_NOW + timedelta(minutes=2),
                lease_until=MERGE_NOW + timedelta(minutes=4),
            )
        assert len(claims) == 1

    gitlab.before_readback = steal_lease
    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        observation_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.merge_request_observation"
        ).scalar_one()
    assert result.effects[0].state is EffectState.RECONCILIATION
    assert result.effects[0].attempts == 3
    assert observation_count == 1
    assert requirement.merged == []
