from dataclasses import replace
from datetime import timedelta

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
from control_plane.app.modules.source_control.adapters import SqlAlchemySourceControlRepository
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

BASE_SHA = "a" * 40
OTHER_SHA = "b" * 40
MESSAGE_ID = "30000000-0000-0000-0000-000000000501"


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
    assert actual_bindings == binding_count


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
