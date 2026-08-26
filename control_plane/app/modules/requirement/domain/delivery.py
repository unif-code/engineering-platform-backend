from control_plane.app.modules.requirement.domain.models import RequirementState, WorkItemState
from control_plane.app.modules.requirement.domain.transitions import InvalidRequirementTransition


def transition_human_work_started(
    requirement: RequirementState,
    work_item: WorkItemState,
) -> tuple[RequirementState, WorkItemState]:
    if requirement is not RequirementState.READY or work_item is not WorkItemState.READY:
        raise InvalidRequirementTransition(f"{requirement.value}/{work_item.value}->IN_PROGRESS")
    return RequirementState.IN_PROGRESS, WorkItemState.IN_PROGRESS


def transition_integration_mr_ready(
    required_work_items: tuple[WorkItemState, ...],
) -> RequirementState:
    if all(state is WorkItemState.VERIFYING for state in required_work_items):
        return RequirementState.VERIFYING
    return RequirementState.IN_PROGRESS
