from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event

import pytest
from sqlalchemy import Engine

from control_plane.app.modules.source_control import (
    EffectState,
    RequirementCallbackState,
    SourceControlDependencies,
    SourceControlEffectDto,
    process_binding_request,
    process_webhook_inbox,
    reconcile_due_effects,
    remove_workspace_repository,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlIntegrationRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.application.reconciliation import (
    _complete_success,
)
from control_plane.app.modules.source_control.application.saga import _effect_dto
from control_plane.app.modules.source_control.ports import (
    GitLabBranchNotFound,
    GitLabProviderUnavailable,
    GitLabResultUnknown,
)
from tests.source_control.test_commands import (
    NOW,
    REPOSITORY_ID,
    FakeEligibility,
    FakeGitLab,
    FakeRequirement,
    _saga_dependencies,
    _seed_binding_request,
)
from tests.source_control.test_integration_merge_saga import (
    NOW as INTEGRATION_MERGE_NOW,
)
from tests.source_control.test_integration_merge_saga import (
    _dependencies as _integration_merge_dependencies,
)
from tests.source_control.test_integration_merge_saga import (
    _seed_merge_request,
    _seed_planned_merge_effect,
)
from tests.source_control.test_integration_mr_saga import (
    NOW as INTEGRATION_CREATE_NOW,
)
from tests.source_control.test_integration_mr_saga import (
    REPOSITORY_ID as INTEGRATION_REPOSITORY_ID,
)
from tests.source_control.test_integration_mr_saga import (
    TASK_BRANCH as INTEGRATION_TASK_BRANCH,
)
from tests.source_control.test_integration_mr_saga import (
    _dependencies as _integration_create_dependencies,
)
from tests.source_control.test_integration_mr_saga import (
    _seed_integration_effect,
    _seed_source_control,
)

BASE_SHA = "a" * 40
OTHER_SHA = "b" * 40
MESSAGE_ID = "30000000-0000-0000-0000-000000000501"


def _accept_mr_webhook(
    engine: Engine,
    dependencies: SourceControlDependencies,
    *,
    inbox_id: str,
    webhook_id: str,
    now: datetime,
    iid: int = 17,
    action: str = "update",
    source_branch: str = INTEGRATION_TASK_BRANCH,
    target_branch: str = "dev",
    state: str = "opened",
) -> None:
    with engine.begin() as db:
        dependencies.repository_factory(db).accept_webhook(
            id=inbox_id,
            repository_id=INTEGRATION_REPOSITORY_ID,
            webhook_id=webhook_id,
            webhook_timestamp=now,
            payload_digest=f"sha256:{webhook_id}",
            provider_event_uuid=None,
            event_type="Merge Request Hook",
            object_kind="merge_request",
            project_id="101",
            ref=None,
            before_sha=None,
            after_sha=None,
            checkout_sha=None,
            mr_iid=iid,
            mr_action=action,
            source_branch=source_branch,
            target_branch=target_branch,
            mr_state=state,
            old_head_sha=None,
            head_sha="c" * 40,
            now=now,
        )


def _unknown_effect(
    engine: Engine,
) -> tuple[
    SourceControlDependencies,
    FakeRequirement,
    FakeGitLab,
    SourceControlEffectDto | None,
]:
    dependencies, requirement, gitlab = _saga_dependencies(engine)
    _seed_binding_request(engine, dependencies)
    gitlab.create_error = GitLabResultUnknown("timeout")
    gitlab.task_read_error = GitLabProviderUnavailable("unreadable")
    result = process_binding_request(message_id=MESSAGE_ID, dependencies=dependencies)
    gitlab.create_error = None
    gitlab.task_read_error = None
    with engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (NOW,),
        )
    return dependencies, requirement, gitlab, result.effect


@pytest.mark.parametrize(
    ("observed_sha", "expected_state", "binding_count"),
    [
        (BASE_SHA, EffectState.SUCCEEDED, 1),
        (OTHER_SHA, EffectState.BLOCKED, 0),
    ],
)
def test_reconciliation_converges_observed_branch(
    isolated_source_control_rw_engine: Engine,
    observed_sha: str,
    expected_state: EffectState,
    binding_count: int,
) -> None:
    dependencies, _requirement, gitlab, _effect = _unknown_effect(isolated_source_control_rw_engine)
    gitlab.branch_sha = observed_sha

    result = reconcile_due_effects(limit=10, dependencies=dependencies)
    with isolated_source_control_rw_engine.connect() as db:
        actual_bindings = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.repository_branch_binding"
        ).scalar_one()

    assert result.effects[0].state is expected_state
    assert result.effects[0].next_reconcile_at is None
    assert actual_bindings == binding_count


def test_stale_reconciliation_lease_cannot_create_binding_after_new_owner_blocks(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, requirement, _gitlab, _effect = _unknown_effect(isolated_source_control_rw_engine)
    with isolated_source_control_rw_engine.begin() as db:
        repository = SqlAlchemySourceControlRepository(db)
        old_claim = repository.claim_unknown_effects(
            limit=1,
            now=NOW,
            lease_until=NOW,
        )[0]
        new_claim = repository.claim_unknown_effects(
            limit=1,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )[0]
        blocked = repository.transition_effect(
            str(new_claim["id"]),
            expected_state=EffectState.RECONCILIATION.value,
            expected_attempts=int(new_claim["attempts"]),
            values={
                "state": EffectState.BLOCKED.value,
                "last_error_code": "OWNER_INELIGIBLE",
                "next_reconcile_at": None,
                "completed_at": NOW,
                "updated_at": NOW,
            },
        )
    assert blocked is not None

    current, binding, completed = _complete_success(
        _effect_dto(old_claim),
        requirement.context,
        dependencies=dependencies,
    )
    with isolated_source_control_rw_engine.connect() as db:
        binding_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.repository_branch_binding"
        ).scalar_one()

    assert completed is False
    assert current.state is EffectState.BLOCKED
    assert binding is None
    assert binding_count == 0


def test_reconciliation_retries_missing_branch_with_same_name_and_base(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, _requirement, gitlab, effect = _unknown_effect(isolated_source_control_rw_engine)
    assert effect is not None
    gitlab.task_read_error_once = GitLabBranchNotFound("missing")

    result = reconcile_due_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.SUCCEEDED
    assert gitlab.created[-1] == (effect.branch_name, effect.base_commit_sha)


def test_second_unknown_returns_to_unknown_with_later_due_time(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, _requirement, gitlab, effect = _unknown_effect(isolated_source_control_rw_engine)
    assert effect is not None
    gitlab.task_read_error = GitLabProviderUnavailable("still unavailable")

    result = reconcile_due_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.UNKNOWN
    assert result.effects[0].next_reconcile_at == NOW + timedelta(seconds=60)


def test_requirement_callback_failure_does_not_undo_external_success(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, requirement, _gitlab, _effect = _unknown_effect(isolated_source_control_rw_engine)
    requirement.fail_next_ready = True

    first = reconcile_due_effects(limit=10, dependencies=dependencies)
    second = reconcile_due_effects(limit=10, dependencies=dependencies)

    assert first.effects[0].state is EffectState.SUCCEEDED
    assert first.effects[0].callback_state is RequirementCallbackState.FAILED
    assert second.effects[0].state is EffectState.SUCCEEDED
    assert second.effects[0].callback_state is RequirementCallbackState.ACKED


@pytest.mark.parametrize(
    ("failure", "safe_reason"),
    [
        ("repository-removed", "REPOSITORY_NOT_AUTHORIZED"),
        ("owner-ineligible", "OWNER_INELIGIBLE"),
    ],
)
def test_reconciliation_stops_new_writes_when_guard_is_no_longer_valid(
    isolated_source_control_rw_engine: Engine,
    failure: str,
    safe_reason: str,
) -> None:
    dependencies, requirement, gitlab, _effect = _unknown_effect(isolated_source_control_rw_engine)
    if failure == "repository-removed":
        with isolated_source_control_rw_engine.begin() as db:
            remove_workspace_repository(
                SqlAlchemySourceControlRepository(db),
                repository_id=REPOSITORY_ID,
                expected_revision=1,
                actor="SYSTEM",
                dependencies=dependencies,
            )
    else:
        dependencies = replace(dependencies, eligibility=FakeEligibility(False))
    gitlab.calls.clear()
    gitlab.created.clear()

    result = reconcile_due_effects(limit=10, dependencies=dependencies)

    assert result.effects[0].state is EffectState.BLOCKED
    assert gitlab.calls == []
    assert gitlab.created == []
    assert requirement.blocked[-1].reason_code == safe_reason


def test_webhook_only_makes_matching_unknown_effect_due(
    isolated_source_control_rw_engine: Engine,
) -> None:
    dependencies, _requirement, _gitlab, effect = _unknown_effect(isolated_source_control_rw_engine)
    assert effect is not None
    future = NOW + timedelta(hours=1)
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s",
            (future,),
        )
        inbox_id = str(dependencies.random.uuid4())
        dependencies.repository_factory(db).accept_webhook(
            id=inbox_id,
            repository_id=REPOSITORY_ID,
            webhook_id="webhook-reconcile-1",
            webhook_timestamp=NOW,
            payload_digest="sha256:webhook-reconcile",
            provider_event_uuid=None,
            event_type="Push Hook",
            object_kind="push",
            project_id="platform/backend",
            ref=f"refs/heads/{effect.branch_name}",
            before_sha="b" * 40,
            after_sha=BASE_SHA,
            checkout_sha=BASE_SHA,
            now=NOW,
        )

    scheduled = process_webhook_inbox(inbox_id, dependencies=dependencies)
    with isolated_source_control_rw_engine.connect() as db:
        binding_count = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.repository_branch_binding"
        ).scalar_one()
        next_due = db.exec_driver_sql(
            "SELECT next_reconcile_at FROM source_control.source_control_effect"
        ).scalar_one()

    assert scheduled == 1
    assert next_due == NOW
    assert binding_count == 0


def test_mr_webhook_makes_unbound_create_effect_due_by_immutable_branch(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_source_control(isolated_source_control_rw_engine)
    _seed_integration_effect(
        isolated_source_control_rw_engine,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, gitlab = _integration_create_dependencies(
        isolated_source_control_rw_engine
    )
    future = INTEGRATION_CREATE_NOW + timedelta(hours=1)
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s "
            "WHERE operation='CREATE_INTEGRATION_MR'",
            (future,),
        )
    inbox_id = "86000000-0000-0000-0000-000000000901"
    _accept_mr_webhook(
        isolated_source_control_rw_engine,
        dependencies,
        inbox_id=inbox_id,
        webhook_id="integration-create-open",
        now=INTEGRATION_CREATE_NOW,
        iid=91,
        action="open",
    )

    scheduled = process_webhook_inbox(inbox_id, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        next_due = db.exec_driver_sql(
            "SELECT next_reconcile_at FROM source_control.source_control_effect "
            "WHERE operation='CREATE_INTEGRATION_MR'"
        ).scalar_one()
        observations = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.merge_request_observation"
        ).scalar_one()
    assert scheduled == 1
    assert next_due == INTEGRATION_CREATE_NOW
    assert observations == 0
    assert gitlab.calls == []


@pytest.mark.parametrize(
    ("action", "mr_state"),
    [
        ("open", "opened"),
        ("update", "opened"),
        ("merge", "merged"),
        ("close", "closed"),
        ("reopen", "opened"),
    ],
)
def test_mr_webhook_actions_make_exact_bound_merge_effect_due_without_get(
    isolated_source_control_rw_engine: Engine,
    action: str,
    mr_state: str,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head="b" * 40,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, _eligibility, gitlab = _integration_merge_dependencies(
        isolated_source_control_rw_engine
    )
    future = INTEGRATION_MERGE_NOW + timedelta(hours=1)
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s "
            "WHERE operation='MERGE_INTEGRATION_MR'",
            (future,),
        )
    inbox_id = "86000000-0000-0000-0000-000000000902"
    _accept_mr_webhook(
        isolated_source_control_rw_engine,
        dependencies,
        inbox_id=inbox_id,
        webhook_id="integration-merge-update",
        now=INTEGRATION_MERGE_NOW,
        action=action,
        state=mr_state,
    )

    scheduled = process_webhook_inbox(inbox_id, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        next_due = db.exec_driver_sql(
            "SELECT next_reconcile_at FROM source_control.source_control_effect "
            "WHERE operation='MERGE_INTEGRATION_MR'"
        ).scalar_one()
        observations = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.merge_request_observation"
        ).scalar_one()
    assert scheduled == 1
    assert next_due == INTEGRATION_MERGE_NOW
    assert observations == 1
    assert gitlab.calls == []


@pytest.mark.parametrize(
    ("iid", "source_branch", "target_branch"),
    [
        (999, INTEGRATION_TASK_BRANCH, "dev"),
        (17, "feat/other", "dev"),
        (17, INTEGRATION_TASK_BRANCH, "main"),
    ],
)
def test_mr_webhook_with_no_exact_integration_effect_schedules_zero(
    isolated_source_control_rw_engine: Engine,
    iid: int,
    source_branch: str,
    target_branch: str,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head="b" * 40,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, _eligibility, _gitlab = _integration_merge_dependencies(
        isolated_source_control_rw_engine
    )
    future = INTEGRATION_MERGE_NOW + timedelta(hours=1)
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s "
            "WHERE operation='MERGE_INTEGRATION_MR'",
            (future,),
        )
    inbox_id = "86000000-0000-0000-0000-000000000903"
    _accept_mr_webhook(
        isolated_source_control_rw_engine,
        dependencies,
        inbox_id=inbox_id,
        webhook_id="integration-merge-mismatch",
        now=INTEGRATION_MERGE_NOW,
        iid=iid,
        source_branch=source_branch,
        target_branch=target_branch,
    )

    scheduled = process_webhook_inbox(inbox_id, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        next_due = db.exec_driver_sql(
            "SELECT next_reconcile_at FROM source_control.source_control_effect "
            "WHERE operation='MERGE_INTEGRATION_MR'"
        ).scalar_one()
    assert scheduled == 0
    assert next_due == future


def test_bound_mr_project_mismatch_does_not_fall_back_to_create_effect(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(
        isolated_source_control_rw_engine,
        external_project_id="different-project",
    )
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head="b" * 40,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, _eligibility, _gitlab = _integration_merge_dependencies(
        isolated_source_control_rw_engine
    )
    future = INTEGRATION_MERGE_NOW + timedelta(hours=1)
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s "
            "WHERE operation='MERGE_INTEGRATION_MR'",
            (future,),
        )
    inbox_id = "86000000-0000-0000-0000-000000000907"
    _accept_mr_webhook(
        isolated_source_control_rw_engine,
        dependencies,
        inbox_id=inbox_id,
        webhook_id="integration-project-mismatch",
        now=INTEGRATION_MERGE_NOW,
    )

    scheduled = process_webhook_inbox(inbox_id, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        next_due = db.exec_driver_sql(
            "SELECT next_reconcile_at FROM source_control.source_control_effect "
            "WHERE operation='MERGE_INTEGRATION_MR'"
        ).scalar_one()
    assert scheduled == 0
    assert next_due == future


@pytest.mark.parametrize("active_state", [EffectState.IN_FLIGHT, EffectState.RECONCILIATION])
def test_mr_webhook_never_shortens_active_integration_lease(
    isolated_source_control_rw_engine: Engine,
    active_state: EffectState,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head="b" * 40,
        state=active_state,
    )
    dependencies, _requirement, _eligibility, _gitlab = _integration_merge_dependencies(
        isolated_source_control_rw_engine
    )
    lease = INTEGRATION_MERGE_NOW + timedelta(minutes=2)
    with isolated_source_control_rw_engine.begin() as db:
        db.exec_driver_sql(
            "UPDATE source_control.source_control_effect SET next_reconcile_at=%s "
            "WHERE operation='MERGE_INTEGRATION_MR'",
            (lease,),
        )
    inbox_id = "86000000-0000-0000-0000-000000000904"
    _accept_mr_webhook(
        isolated_source_control_rw_engine,
        dependencies,
        inbox_id=inbox_id,
        webhook_id="integration-active-lease",
        now=INTEGRATION_MERGE_NOW,
    )

    scheduled = process_webhook_inbox(inbox_id, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        next_due = db.exec_driver_sql(
            "SELECT next_reconcile_at FROM source_control.source_control_effect "
            "WHERE operation='MERGE_INTEGRATION_MR'"
        ).scalar_one()
    assert scheduled == 0
    assert next_due == lease


def test_webhook_losing_race_to_effect_claim_cannot_shorten_new_lease(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head="b" * 40,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, _eligibility, _gitlab = _integration_merge_dependencies(
        isolated_source_control_rw_engine
    )
    inbox_id = "86000000-0000-0000-0000-000000000908"
    _accept_mr_webhook(
        isolated_source_control_rw_engine,
        dependencies,
        inbox_id=inbox_id,
        webhook_id="integration-webhook-claim-race",
        now=INTEGRATION_MERGE_NOW,
    )
    due_update_entered = Event()

    class SignalingRepository(SqlAlchemySourceControlRepository):
        def make_integration_effect_due(
            self,
            *,
            repository_id: str,
            project_id: str,
            mr_iid: int,
            source_branch: str,
            target_branch: str,
            now: datetime,
        ) -> int:
            due_update_entered.set()
            return super().make_integration_effect_due(
                repository_id=repository_id,
                project_id=project_id,
                mr_iid=mr_iid,
                source_branch=source_branch,
                target_branch=target_branch,
                now=now,
            )

    dependencies = replace(
        dependencies,
        repository_factory=SignalingRepository,
    )
    lease = INTEGRATION_MERGE_NOW + timedelta(minutes=2)
    owner_connection = isolated_source_control_rw_engine.connect()
    owner_transaction = owner_connection.begin()
    try:
        claims = SqlAlchemySourceControlIntegrationRepository(owner_connection).claim_effects(
            limit=1,
            now=INTEGRATION_MERGE_NOW,
            lease_until=lease,
        )
        assert len(claims) == 1
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                process_webhook_inbox,
                inbox_id,
                dependencies=dependencies,
            )
            assert due_update_entered.wait(timeout=5)
            assert not future.done()
            owner_transaction.commit()
            scheduled = future.result(timeout=5)
    finally:
        if owner_transaction.is_active:
            owner_transaction.rollback()
        owner_connection.close()

    with isolated_source_control_rw_engine.connect() as db:
        effect = db.exec_driver_sql(
            "SELECT state, next_reconcile_at FROM "
            "source_control.source_control_effect "
            "WHERE operation='MERGE_INTEGRATION_MR'"
        ).one()
    assert scheduled == 0
    assert effect[0] == EffectState.RECONCILIATION.value
    assert effect[1] == lease


def test_out_of_order_mr_webhooks_only_repeat_unknown_due_hint(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _seed_merge_request(isolated_source_control_rw_engine)
    _seed_planned_merge_effect(
        isolated_source_control_rw_engine,
        frozen_head="b" * 40,
        state=EffectState.UNKNOWN,
    )
    dependencies, _requirement, _eligibility, gitlab = _integration_merge_dependencies(
        isolated_source_control_rw_engine
    )
    first_id = "86000000-0000-0000-0000-000000000905"
    second_id = "86000000-0000-0000-0000-000000000906"
    _accept_mr_webhook(
        isolated_source_control_rw_engine,
        dependencies,
        inbox_id=first_id,
        webhook_id="integration-out-of-order-merge",
        now=INTEGRATION_MERGE_NOW,
        action="merge",
        state="merged",
    )
    _accept_mr_webhook(
        isolated_source_control_rw_engine,
        dependencies,
        inbox_id=second_id,
        webhook_id="integration-out-of-order-update",
        now=INTEGRATION_MERGE_NOW,
        action="update",
    )

    first = process_webhook_inbox(first_id, dependencies=dependencies)
    second = process_webhook_inbox(second_id, dependencies=dependencies)

    with isolated_source_control_rw_engine.connect() as db:
        observations = db.exec_driver_sql(
            "SELECT count(*) FROM source_control.merge_request_observation"
        ).scalar_one()
    assert (first, second) == (1, 1)
    assert observations == 1
    assert gitlab.calls == []
