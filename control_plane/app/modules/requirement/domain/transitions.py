from control_plane.app.modules.requirement.domain.models import (
    AssignmentState,
    RepositoryState,
    RequirementState,
    WorkItemState,
)


class RequirementError(ValueError):
    """A deterministic Requirement workflow denial."""


class InvalidRequirementTransition(RequirementError):
    pass


class InvalidRequirementInput(RequirementError):
    pass


class RequirementNotFound(RequirementError):
    pass


class StaleRequirementRevision(RequirementError):
    pass


class RepositoryBindingRequestMissing(RequirementError):
    pass


_FIRST_BATCH_TRANSITIONS = {
    RequirementState.CREATED: {RequirementState.PREPARING, RequirementState.CANCELED},
    RequirementState.PREPARING: {
        RequirementState.AWAITING_CONFIRMATION,
        RequirementState.CANCELED,
    },
    RequirementState.AWAITING_CONFIRMATION: {
        RequirementState.READY,
        RequirementState.PREPARING,
        RequirementState.CANCELED,
    },
    RequirementState.READY: {RequirementState.CANCELED},
    RequirementState.CANCELED: set(),
}


def transition_requirement(
    current: RequirementState,
    target: RequirementState,
) -> RequirementState:
    if target not in _FIRST_BATCH_TRANSITIONS[current]:
        raise InvalidRequirementTransition(f"{current.value}->{target.value}")
    return target


def derive_work_item_state(
    assignment: AssignmentState,
    repository: RepositoryState,
) -> WorkItemState:
    if assignment is AssignmentState.ASSIGNED and repository is RepositoryState.BOUND:
        return WorkItemState.READY
    return WorkItemState.DRAFT
