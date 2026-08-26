import pytest

from control_plane.app.modules.requirement.domain import (
    IntegrationDeliveryBlockedReason,
    IntegrationDeliveryRequestKind,
    IntegrationDeliveryState,
    InvalidRequirementTransition,
    RequirementState,
    WorkItemState,
    transition_human_work_started,
    transition_integration_mr_ready,
)


def test_delivery_types_expose_the_stable_business_contract() -> None:
    assert tuple(IntegrationDeliveryState) == (
        IntegrationDeliveryState.NOT_STARTED,
        IntegrationDeliveryState.IMPLEMENTING,
        IntegrationDeliveryState.MR_PENDING,
        IntegrationDeliveryState.MR_OPEN,
        IntegrationDeliveryState.MERGE_PENDING,
        IntegrationDeliveryState.INTEGRATED,
        IntegrationDeliveryState.BLOCKED,
        IntegrationDeliveryState.RECONCILIATION_PENDING,
    )
    assert tuple(IntegrationDeliveryRequestKind) == (
        IntegrationDeliveryRequestKind.CREATE_MR,
        IntegrationDeliveryRequestKind.MERGE_MR,
    )
    assert IntegrationDeliveryBlockedReason.MR_CONFLICT.value == "MR_CONFLICT"


def test_human_start_requires_ready_requirement_and_work_item() -> None:
    assert transition_human_work_started(
        RequirementState.READY,
        WorkItemState.READY,
    ) == (RequirementState.IN_PROGRESS, WorkItemState.IN_PROGRESS)

    with pytest.raises(InvalidRequirementTransition):
        transition_human_work_started(
            RequirementState.PREPARING,
            WorkItemState.READY,
        )


def test_mr_ready_keeps_requirement_in_progress_until_all_required_items_verify() -> None:
    assert (
        transition_integration_mr_ready(
            (WorkItemState.VERIFYING, WorkItemState.IN_PROGRESS),
        )
        is RequirementState.IN_PROGRESS
    )
    assert (
        transition_integration_mr_ready(
            (WorkItemState.VERIFYING, WorkItemState.VERIFYING),
        )
        is RequirementState.VERIFYING
    )
