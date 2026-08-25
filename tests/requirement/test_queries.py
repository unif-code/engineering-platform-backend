import pytest

from control_plane.app.modules.requirement import (
    InvalidRequirementCursor,
    RequirementState,
    RequirementType,
    create_requirement,
    get_requirement,
    list_requirements,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_commands import WORKSPACE_ID, Actor, _create, _dependencies


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
    with isolated_requirement_database.runtime.connect() as db, pytest.raises(
        InvalidRequirementCursor
    ):
        list_requirements(
            db,
            workspace_id=WORKSPACE_ID,
            cursor="not-a-cursor",
            limit=20,
            dependencies=_dependencies(),
        )
