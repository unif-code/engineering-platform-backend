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
    get_requirement as _get_requirement,
)
from control_plane.app.modules.requirement.application import (
    list_requirements as _list_requirements,
)
from control_plane.app.modules.requirement.application import (
    record_repository_binding as _record_repository_binding,
)
from control_plane.app.modules.requirement.application import (
    start_requirement_preparation as _start_requirement_preparation,
)
from control_plane.app.modules.requirement.domain import (
    AssignmentState,
    CreateRequirementResult,
    ExecutorType,
    InvalidRequirementCursor,
    InvalidRequirementInput,
    InvalidRequirementTransition,
    RecordState,
    RepositoryBindingConflict,
    RepositoryBindingRequestMissing,
    RepositoryState,
    RequirementDetailsDto,
    RequirementDto,
    RequirementError,
    RequirementNotFound,
    RequirementPage,
    RequirementState,
    RequirementType,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    WorkItemDto,
    WorkItemNotFound,
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


def get_requirement(
    db: Connection,
    *,
    requirement_id: str,
    dependencies: RequirementDependencies,
) -> RequirementDetailsDto:
    return _get_requirement(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
    )


def list_requirements(
    db: Connection,
    *,
    workspace_id: str,
    cursor: str | None,
    limit: int,
    dependencies: RequirementDependencies,
) -> RequirementPage:
    return _list_requirements(
        dependencies.repository_factory(db),
        workspace_id=workspace_id,
        cursor=cursor,
        limit=limit,
    )


def record_repository_binding(
    db: Connection,
    *,
    work_item_id: str,
    repository_id: str,
    base_commit_sha: str,
    task_branch: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDto:
    return _record_repository_binding(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        repository_id=repository_id,
        base_commit_sha=base_commit_sha,
        task_branch=task_branch,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


__all__ = [
    "AssignmentState",
    "CreateRequirementResult",
    "ExecutorType",
    "InvalidRequirementCursor",
    "InvalidRequirementInput",
    "InvalidRequirementTransition",
    "RecordState",
    "RepositoryBindingConflict",
    "RepositoryBindingRequestMissing",
    "RepositoryState",
    "RequirementDependencies",
    "RequirementDto",
    "RequirementDetailsDto",
    "RequirementError",
    "RequirementNotFound",
    "RequirementPage",
    "RequirementState",
    "RequirementType",
    "StaleRequirementRevision",
    "StaleWorkItemRevision",
    "WorkItemDto",
    "WorkItemNotFound",
    "WorkItemState",
    "create_requirement",
    "derive_work_item_state",
    "get_requirement",
    "list_requirements",
    "record_repository_binding",
    "start_requirement_preparation",
    "transition_requirement",
]
