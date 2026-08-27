import pytest
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    InvalidRequirementInput,
    RepositoryBindingBlockedReason,
    RepositoryState,
    WorkItemState,
    record_repository_binding,
    record_repository_binding_blocked,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_commands import Actor, _create, _dependencies


def test_binding_makes_assigned_work_item_ready_and_replays_exact_result(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(isolated_requirement_database, idempotency_key="binding-create")

    with isolated_requirement_database.runtime.begin() as db:
        first = record_repository_binding(
            db,
            work_item_id=created.work_item.id,
            repository_id="repository-1",
            base_commit_sha="a" * 40,
            task_branch="work-items/first",
            expected_revision=1,
            actor=Actor("SYSTEM"),
            idempotency_key="binding-ready-0001",
            correlation_id="source-control:effect:binding-ready-0001",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = record_repository_binding(
            db,
            work_item_id=created.work_item.id,
            repository_id="repository-1",
            base_commit_sha="a" * 40,
            task_branch="work-items/first",
            expected_revision=1,
            actor=Actor("SYSTEM"),
            idempotency_key="binding-ready-0001",
            correlation_id="source-control:effect:binding-ready-replay",
            dependencies=_dependencies(),
        )

    assert first.repository_state is RepositoryState.BOUND
    assert first.state is WorkItemState.READY
    assert first.revision == 2
    assert replay == first
    with isolated_requirement_database.owner.connect() as db:
        events = (
            db.execute(
                text(
                    "SELECT correlation_id FROM audit.audit_event "
                    "WHERE target_id=:work_item_id "
                    "AND action='requirement.repository_binding.recorded'"
                ),
                {"work_item_id": created.work_item.id},
            )
            .scalars()
            .all()
        )
    assert events == ["source-control:effect:binding-ready-0001"]


def test_binding_keeps_unassigned_work_item_draft(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(
        isolated_requirement_database,
        auto_assign=False,
        idempotency_key="binding-unassigned-create",
    )

    with isolated_requirement_database.runtime.begin() as db:
        bound = record_repository_binding(
            db,
            work_item_id=created.work_item.id,
            repository_id="repository-1",
            base_commit_sha="b" * 40,
            task_branch="work-items/unassigned",
            expected_revision=1,
            actor=Actor("SYSTEM"),
            idempotency_key="binding-unassigned-0001",
            correlation_id="source-control:effect:binding-unassigned-0001",
            dependencies=_dependencies(),
        )

    assert bound.repository_state is RepositoryState.BOUND
    assert bound.state is WorkItemState.DRAFT


def test_binding_records_a_structured_block_and_recovers_the_same_work_item(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(
        isolated_requirement_database,
        idempotency_key="binding-blocked-create",
    )
    blocked_correlation = f"source-control:work-item:{created.work_item.id}"
    ready_correlation = "source-control:effect:binding-recovered-0001"

    with isolated_requirement_database.runtime.begin() as db:
        blocked = record_repository_binding_blocked(
            db,
            work_item_id=created.work_item.id,
            repository_id="repository-1",
            reason_code=RepositoryBindingBlockedReason.CONNECTOR_UNAVAILABLE,
            expected_revision=1,
            actor=Actor("SYSTEM"),
            idempotency_key="binding-blocked-0001",
            correlation_id=blocked_correlation,
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        recovered = record_repository_binding(
            db,
            work_item_id=created.work_item.id,
            repository_id="repository-1",
            base_commit_sha="c" * 40,
            task_branch="work-items/recovered",
            expected_revision=blocked.revision,
            actor=Actor("SYSTEM"),
            idempotency_key="binding-recovered-0001",
            correlation_id=ready_correlation,
            dependencies=_dependencies(),
        )

    assert blocked.repository_state is RepositoryState.BLOCKED
    assert blocked.repository_blocked_reason_code is (
        RepositoryBindingBlockedReason.CONNECTOR_UNAVAILABLE
    )
    assert blocked.repository_blocked_at is not None
    assert blocked.state is WorkItemState.DRAFT
    assert recovered.repository_state is RepositoryState.BOUND
    assert recovered.repository_blocked_reason_code is None
    assert recovered.repository_blocked_at is None
    assert recovered.state is WorkItemState.READY
    assert recovered.revision == 3
    with isolated_requirement_database.owner.connect() as db:
        events = list(
            db.execute(
                text(
                    "SELECT action, correlation_id FROM audit.audit_event "
                    "WHERE target_id=:work_item_id "
                    "AND action LIKE 'requirement.repository_binding.%' ORDER BY occurred_at"
                ),
                {"work_item_id": created.work_item.id},
            ).all()
        )
    assert events == [
        ("requirement.repository_binding.blocked", blocked_correlation),
        ("requirement.repository_binding.recorded", ready_correlation),
    ]


def test_binding_callback_rejects_blank_correlation(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(isolated_requirement_database, idempotency_key="binding-blank-create")

    with pytest.raises(InvalidRequirementInput):
        with isolated_requirement_database.runtime.begin() as db:
            record_repository_binding(
                db,
                work_item_id=created.work_item.id,
                repository_id="repository-1",
                base_commit_sha="a" * 40,
                task_branch="work-items/blank",
                expected_revision=1,
                actor=Actor("SYSTEM"),
                idempotency_key="binding-blank-correlation",
                correlation_id=" ",
                dependencies=_dependencies(),
            )
