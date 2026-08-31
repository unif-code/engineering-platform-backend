import pytest
from pydantic import ValidationError
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    InvalidRequirementCursor,
    RequirementDependencyUnavailable,
    RequirementState,
    RequirementType,
    add_work_item,
    create_requirement,
    get_requirement,
    get_requirement_delivery_snapshot,
    list_requirements,
    start_requirement_preparation,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_commands import WORKSPACE_ID, Actor, _create, _dependencies
from tests.requirement.test_planning_commands import _set_hash


def test_get_requirement_returns_aggregate_and_initial_work_item(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(isolated_requirement_database, idempotency_key="query-detail-create")

    with isolated_requirement_database.runtime.connect() as db:
        detail = get_requirement(
            db,
            requirement_id=created.requirement.id,
            dependencies=_dependencies(),
        )

    assert detail.requirement == created.requirement
    assert detail.work_items == (created.work_item,)


def test_delivery_snapshot_is_a_deterministic_immutable_current_read_model(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(
        isolated_requirement_database,
        idempotency_key="query-delivery-snapshot-create",
    )
    with isolated_requirement_database.runtime.connect() as db:
        original = get_requirement_delivery_snapshot(
            db,
            requirement_id=created.requirement.id,
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        prepared = start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=created.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="query-delivery-snapshot-prepare",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        added = add_work_item(
            db,
            requirement_id=created.requirement.id,
            repository_id="repository-2",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="query-delivery-snapshot-add",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.connect() as db:
        current = get_requirement_delivery_snapshot(
            db,
            requirement_id=created.requirement.id,
            dependencies=_dependencies(),
        )
        replay = get_requirement_delivery_snapshot(
            db,
            requirement_id=created.requirement.id,
            dependencies=_dependencies(),
        )

    assert original.work_item_ids == (created.work_item.id,)
    assert original.required_work_item_set_version == 1
    assert current == replay
    assert current.requirement_id == created.requirement.id
    assert current.requirement_version == added.requirement.requirement_version
    assert current.required_work_item_set_version == 2
    assert current.work_item_ids == tuple(sorted((created.work_item.id, added.work_item.id)))
    assert current.required_work_item_set_hash == _set_hash(list(current.work_item_ids))
    assert original.work_item_ids == (created.work_item.id,)
    with pytest.raises(ValidationError, match="frozen"):
        current.requirement_version = 99


def test_delivery_snapshot_fails_closed_when_the_set_hash_does_not_match_its_ids(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(
        isolated_requirement_database,
        idempotency_key="query-delivery-snapshot-corrupt",
    )
    with isolated_requirement_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.requirement "
                "SET required_work_item_set_hash=:hash WHERE id=:requirement_id"
            ),
            {
                "hash": _set_hash(["00000000-0000-0000-0000-000000000099"]),
                "requirement_id": created.requirement.id,
            },
        )

    with (
        isolated_requirement_database.runtime.connect() as db,
        pytest.raises(RequirementDependencyUnavailable, match="delivery snapshot"),
    ):
        get_requirement_delivery_snapshot(
            db,
            requirement_id=created.requirement.id,
            dependencies=_dependencies(),
        )


def test_list_requirements_is_workspace_scoped_and_uses_stable_cursor(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    first_created = _create(isolated_requirement_database, idempotency_key="query-list-one")
    second_created = _create(isolated_requirement_database, idempotency_key="query-list-two")
    with isolated_requirement_database.runtime.begin() as db:
        create_requirement(
            db,
            workspace_id="20000000-0000-0000-0000-000000000399",
            requirement_type=RequirementType.FEAT,
            title="Other workspace",
            description="Must not leak into the requested workspace.",
            acceptance_criteria=("isolated",),
            initial_repository_id="repository-1",
            actor=Actor("employee-1"),
            idempotency_key="query-list-other-workspace",
            dependencies=_dependencies(),
        )
    expected_ids = sorted([first_created.requirement.id, second_created.requirement.id])

    with isolated_requirement_database.runtime.connect() as db:
        first_page = list_requirements(
            db,
            workspace_id=WORKSPACE_ID,
            cursor=None,
            limit=1,
            dependencies=_dependencies(),
        )
        second_page = list_requirements(
            db,
            workspace_id=WORKSPACE_ID,
            cursor=first_page.next_cursor,
            limit=1,
            dependencies=_dependencies(),
        )

    assert [item.id for item in first_page.items] == expected_ids[:1]
    assert first_page.next_cursor is not None
    assert [item.id for item in second_page.items] == expected_ids[1:]
    assert second_page.next_cursor is None
    assert all(item.state is RequirementState.CREATED for item in first_page.items)


def test_list_requirements_rejects_malformed_cursor(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    with (
        isolated_requirement_database.runtime.connect() as db,
        pytest.raises(InvalidRequirementCursor),
    ):
        list_requirements(
            db,
            workspace_id=WORKSPACE_ID,
            cursor="not-a-cursor",
            limit=20,
            dependencies=_dependencies(),
        )
