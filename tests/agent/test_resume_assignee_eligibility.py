from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

from control_plane.app.modules import requirement
from control_plane.app.modules.agent.adapters import requirement as adapter_module
from control_plane.app.modules.agent.adapters.requirement import RequirementFacadeExecutionContext
from control_plane.app.modules.agent.application.errors import InvalidRequirementExecutionContext
from control_plane.app.modules.agent.ports.runtime import RequirementExecutionRequest
from control_plane.app.modules.requirement import (
    RequirementDependencyUnavailable,
    WorkItemAssigneeIneligible,
)
from tests.agent.test_start_run import (
    ASSIGNMENT_ID,
    REQUIREMENT_ID,
    WORK_ITEM_ID,
    WORKSPACE_ID,
    _assignment,
    _requirement_details,
)


@pytest.fixture
def owner() -> SimpleNamespace:
    repository = Mock()
    repository.requirement_by_id.return_value = {"id": REQUIREMENT_ID, "workspace_id": WORKSPACE_ID}
    repository.work_item_by_id.return_value = {
        "id": WORK_ITEM_ID,
        "requirement_id": REQUIREMENT_ID,
        "human_owner_id": "selected-person",
        "repository_id": "repository-current",
        "required_capabilities": ["code.change", "work_item.execute"],
        "created_by": "different-creator",
    }
    repository.current_work_item_assignment.return_value = {
        "id": ASSIGNMENT_ID,
        "work_item_id": WORK_ITEM_ID,
        "assignee_id": "selected-person",
        "superseded_at": None,
    }
    guard = SimpleNamespace(
        can_assign=Mock(return_value=True),
        can_auto_assign=Mock(side_effect=AssertionError("resume cannot use automatic fallback")),
    )
    dependencies = Mock(repository_factory=Mock(return_value=repository), assignment_guard=guard)
    return SimpleNamespace(repository=repository, guard=guard, dependencies=dependencies)


def qualify(owner: Any) -> None:
    assert hasattr(requirement, "assert_work_item_assignee_eligible"), (
        "owner eligibility Facade is missing"
    )
    requirement.assert_work_item_assignee_eligible(
        Mock(),
        requirement_id=REQUIREMENT_ID,
        work_item_id=WORK_ITEM_ID,
        expected_assignment_id=ASSIGNMENT_ID,
        dependencies=owner.dependencies,
    )


def test_explicit_guard_uses_current_selected_assignee_and_owner_parameters(owner: Any) -> None:
    qualify(owner)
    owner.guard.can_assign.assert_called_once_with(
        actor_id="selected-person",
        workspace_id=WORKSPACE_ID,
        repository_id="repository-current",
        required_capabilities=("code.change", "work_item.execute"),
    )
    owner.guard.can_auto_assign.assert_not_called()


@pytest.mark.parametrize("result", [False, None, 1, "yes"])
def test_only_explicit_boolean_result_is_trusted(owner: Any, result: Any) -> None:
    owner.guard.can_assign.return_value = result
    with pytest.raises(
        WorkItemAssigneeIneligible if result is False else RequirementDependencyUnavailable
    ):
        qualify(owner)
    owner.guard.can_auto_assign.assert_not_called()


def test_missing_explicit_guard_never_falls_back_to_automatic_assignment(owner: Any) -> None:
    automatic = Mock(return_value=True)
    owner.dependencies.assignment_guard = SimpleNamespace(can_auto_assign=automatic)
    with pytest.raises(RequirementDependencyUnavailable):
        qualify(owner)
    automatic.assert_not_called()


@pytest.mark.parametrize(
    ("row", "field", "value"),
    [
        ("work", "human_owner_id", None),
        ("work", "human_owner_id", " "),
        ("work", "human_owner_id", "another-person"),
        ("assignment", "assignee_id", ""),
        ("assignment", "assignee_id", 9),
        ("assignment", "assignee_id", " selected-person "),
        ("assignment", "id", "different-assignment"),
        ("assignment", "work_item_id", "different-work-item"),
        ("assignment", "superseded_at", "already-superseded"),
        ("work", "requirement_id", "another-requirement"),
        ("work", "repository_id", ""),
        ("work", "required_capabilities", "code.change"),
        ("work", "required_capabilities", [None]),
    ],
)
def test_invalid_owner_targets_cannot_become_positive_or_negative_eligibility(
    owner: Any, row: str, field: str, value: Any
) -> None:
    target = (
        owner.repository.work_item_by_id
        if row == "work"
        else owner.repository.current_work_item_assignment
    )
    target.return_value[field] = value
    with pytest.raises(RequirementDependencyUnavailable):
        qualify(owner)
    owner.guard.can_assign.assert_not_called()


def test_explicit_dependency_failure_is_not_false(owner: Any) -> None:
    owner.guard.can_assign.side_effect = RuntimeError("synthetic dependency unavailable")
    with pytest.raises(RuntimeError, match="synthetic dependency unavailable"):
        qualify(owner)
    owner.guard.can_auto_assign.assert_not_called()


def test_unbound_guard_keeps_automatic_unassigned_and_explicit_unavailable() -> None:
    from control_plane.app.modules.requirement.adapters.runtime import (
        FailClosedAutomaticAssignmentGuard,
    )

    guard = FailClosedAutomaticAssignmentGuard()
    values: dict[str, Any] = dict(
        actor_id="selected-person",
        workspace_id=WORKSPACE_ID,
        repository_id="repository-current",
        required_capabilities=("code.change",),
    )
    assert guard.can_auto_assign(**values) is False
    with pytest.raises(RequirementDependencyUnavailable):
        guard.can_assign(**values)


@pytest.mark.parametrize("mismatch", [False, True])
def test_adapter_checks_saved_assignment_before_eligibility_under_parent_protection(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: bool,
) -> None:
    details = _requirement_details(assignments=(_assignment(),))
    if mismatch:
        details = replace(details, work_item_assignments=(_assignment(assignment_id=WORK_ITEM_ID),))
    monkeypatch.setattr(adapter_module, "get_requirement_for_update", Mock(return_value=details))
    monkeypatch.setattr(adapter_module, "get_requirement", Mock(return_value=details))
    db = MagicMock()
    deps = Mock()
    guard = Mock()
    monkeypatch.setattr(adapter_module, "assert_work_item_assignee_eligible", guard, raising=False)
    adapter = RequirementFacadeExecutionContext(db, deps)
    request = RequirementExecutionRequest(
        workspace_id=WORKSPACE_ID, requirement_id=REQUIREMENT_ID, work_item_id=WORK_ITEM_ID
    )
    adapter.resolve(request)
    guard.assert_not_called()
    if mismatch:
        with pytest.raises(InvalidRequirementExecutionContext) as error:
            with adapter.protect(request, expected_assignment_id=ASSIGNMENT_ID):
                pytest.fail("changed Assignment cannot reach protected resume")
        assert error.value.reason == "ASSIGNMENT_CHANGED"
        guard.assert_not_called()
    else:
        with adapter.protect(request, expected_assignment_id=ASSIGNMENT_ID) as current:
            assert current.assignment_id == ASSIGNMENT_ID
            db.begin.return_value.__exit__.assert_not_called()
            guard.assert_called_once_with(
                db,
                requirement_id=REQUIREMENT_ID,
                work_item_id=WORK_ITEM_ID,
                expected_assignment_id=ASSIGNMENT_ID,
                dependencies=deps,
            )
    db.begin.return_value.__exit__.assert_called_once()
