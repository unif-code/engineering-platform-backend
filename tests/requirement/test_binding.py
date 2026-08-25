from control_plane.app.modules.requirement import (
    RepositoryState,
    WorkItemState,
    record_repository_binding,
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
            dependencies=_dependencies(),
        )

    assert first.repository_state is RepositoryState.BOUND
    assert first.state is WorkItemState.READY
    assert first.revision == 2
    assert replay == first


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
            dependencies=_dependencies(),
        )

    assert bound.repository_state is RepositoryState.BOUND
    assert bound.state is WorkItemState.DRAFT
