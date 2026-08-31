from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.app.modules.requirement.adapters import V04RouteSnapshotCatalog
from control_plane.app.modules.requirement.domain import (
    AssignmentState,
    InvalidRequirementTransition,
    RecordState,
    RepositoryState,
    RequirementDto,
    RequirementState,
    RequirementType,
    WorkItemState,
    derive_work_item_state,
    transition_human_work_started,
    transition_requirement,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RequirementState.CREATED, RequirementState.PREPARING),
        (RequirementState.CREATED, RequirementState.CANCELED),
        (RequirementState.PREPARING, RequirementState.AWAITING_CONFIRMATION),
        (RequirementState.PREPARING, RequirementState.CANCELED),
        (RequirementState.AWAITING_CONFIRMATION, RequirementState.READY),
        (RequirementState.AWAITING_CONFIRMATION, RequirementState.PREPARING),
        (RequirementState.AWAITING_CONFIRMATION, RequirementState.CANCELED),
        (RequirementState.READY, RequirementState.CANCELED),
    ],
)
def test_requirement_allows_only_first_batch_forward_and_controlled_cancel_transitions(
    current: RequirementState,
    target: RequirementState,
) -> None:
    assert transition_requirement(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RequirementState.CREATED, RequirementState.READY),
        (RequirementState.PREPARING, RequirementState.READY),
        (RequirementState.READY, RequirementState.PREPARING),
        (RequirementState.CANCELED, RequirementState.PREPARING),
        (RequirementState.CANCELED, RequirementState.READY),
    ],
)
def test_requirement_rejects_skipped_reopened_and_terminal_transitions(
    current: RequirementState,
    target: RequirementState,
) -> None:
    with pytest.raises(InvalidRequirementTransition, match=f"{current.value}->{target.value}"):
        transition_requirement(current, target)


@pytest.mark.parametrize(
    ("requirement", "assignment", "repository", "expected"),
    [
        (
            RequirementState.PREPARING,
            AssignmentState.ASSIGNED,
            RepositoryState.BOUND,
            WorkItemState.DRAFT,
        ),
        (
            RequirementState.READY,
            AssignmentState.UNASSIGNED,
            RepositoryState.BOUND,
            WorkItemState.DRAFT,
        ),
        (
            RequirementState.READY,
            AssignmentState.ASSIGNED,
            RepositoryState.WAITING_REPOSITORY,
            WorkItemState.DRAFT,
        ),
        (
            RequirementState.READY,
            AssignmentState.ASSIGNED,
            RepositoryState.BOUND,
            WorkItemState.READY,
        ),
    ],
)
def test_work_item_is_ready_only_after_the_requirement_gate_and_local_guards_are_ready(
    requirement: RequirementState,
    assignment: AssignmentState,
    repository: RepositoryState,
    expected: WorkItemState,
) -> None:
    assert derive_work_item_state(requirement, assignment, repository) is expected


@pytest.mark.parametrize(
    "requirement",
    (RequirementState.READY, RequirementState.IN_PROGRESS),
)
def test_ready_work_item_can_start_before_or_after_a_sibling_has_started(
    requirement: RequirementState,
) -> None:
    assert transition_human_work_started(requirement, WorkItemState.READY) == (
        RequirementState.IN_PROGRESS,
        WorkItemState.IN_PROGRESS,
    )


def test_v04_route_snapshot_freezes_ordered_delivery_steps_and_capabilities() -> None:
    catalog = V04RouteSnapshotCatalog()

    feature = catalog.current(RequirementType.FEAT)
    fix = catalog.current(RequirementType.FIX)

    assert feature.version == 2
    assert feature.requirement_type is RequirementType.FEAT
    assert feature.required_capabilities == ("code.change",)
    assert feature.steps == (
        "brainstorming",
        "writing-plans",
        "test-driven-development",
        "verification-before-completion",
        "requesting-code-review",
    )
    assert feature.snapshot_hash.startswith("sha256:")
    assert catalog.current(RequirementType.FEAT) == feature
    assert fix.steps[0] == "systematic-debugging"
    assert fix.snapshot_hash != feature.snapshot_hash


def test_requirement_dto_is_an_immutable_domain_snapshot() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    requirement = RequirementDto(
        id="10000000-0000-0000-0000-000000000001",
        workspace_id="20000000-0000-0000-0000-000000000001",
        type=RequirementType.FEAT,
        title="Govern manual delivery",
        description="Create the first auditable requirement baseline.",
        acceptance_criteria=("the baseline is approved",),
        created_by="employee-1",
        initial_repository_id="gitlab-project-1",
        route_snapshot_version=1,
        route_snapshot_hash="sha256:route-1",
        route_snapshot={
            "requirementType": "feat",
            "requiredCapabilities": ["code.change"],
            "version": 1,
        },
        state=RequirementState.CREATED,
        record_state=RecordState.ACTIVE,
        requirement_version=1,
        required_work_item_set_version=1,
        required_work_item_set_hash="sha256:work-items-1",
        current_sdd_baseline_id=None,
        revision=1,
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(ValidationError, match="frozen"):
        requirement.state = RequirementState.READY
