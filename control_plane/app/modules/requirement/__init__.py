"""Public Requirement facade; other modules must not import internals."""

from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.requirement.application import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.application import (
    create_requirement as _create_requirement,
)
from control_plane.app.modules.requirement.application import (
    start_requirement_preparation as _start_requirement_preparation,
)
from control_plane.app.modules.requirement.domain import (
    AssignmentState,
    CreateRequirementResult,
    ExecutorType,
    InvalidRequirementInput,
    InvalidRequirementTransition,
    RecordState,
    RepositoryBindingRequestMissing,
    RepositoryState,
    RequirementDto,
    RequirementError,
    RequirementNotFound,
    RequirementState,
    RequirementType,
    StaleRequirementRevision,
    WorkItemDto,
    WorkItemState,
    derive_work_item_state,
    transition_requirement,
)


def create_requirement(
    db: Connection,
    *,
    workspace_id: str,
    requirement_type: RequirementType,
    title: str,
    description: str,
    acceptance_criteria: tuple[str, ...],
    initial_repository_id: str,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> CreateRequirementResult:
    return _create_requirement(
        dependencies.repository_factory(db),
        workspace_id=workspace_id,
        requirement_type=requirement_type,
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        initial_repository_id=initial_repository_id,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def start_requirement_preparation(
    db: Connection,
    *,
    requirement_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> RequirementDto:
    return _start_requirement_preparation(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


__all__ = [
    "AssignmentState",
    "CreateRequirementResult",
    "ExecutorType",
    "InvalidRequirementInput",
    "InvalidRequirementTransition",
    "RecordState",
    "RepositoryBindingRequestMissing",
    "RepositoryState",
    "RequirementDependencies",
    "RequirementDto",
    "RequirementError",
    "RequirementNotFound",
    "RequirementState",
    "RequirementType",
    "StaleRequirementRevision",
    "WorkItemDto",
    "WorkItemState",
    "create_requirement",
    "derive_work_item_state",
    "start_requirement_preparation",
    "transition_requirement",
]
