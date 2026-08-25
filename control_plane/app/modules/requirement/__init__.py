"""Public Requirement facade; other modules must not import internals."""

from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.requirement.application import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.application import (
    create_requirement as _create_requirement,
)
from control_plane.app.modules.requirement.application import decide_baseline as _decide_baseline
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
    record_repository_binding_blocked as _record_repository_binding_blocked,
)
from control_plane.app.modules.requirement.application import (
    register_sdd_baseline as _register_sdd_baseline,
)
from control_plane.app.modules.requirement.application import (
    start_requirement_preparation as _start_requirement_preparation,
)
from control_plane.app.modules.requirement.application import (
    submit_baseline_confirmation as _submit_baseline_confirmation,
)
from control_plane.app.modules.requirement.domain import (
    ArtifactUnavailable,
    AssignmentState,
    BaselineConfirmationResult,
    BaselineDecisionResult,
    CreateRequirementResult,
    DecisionDto,
    DecisionOutcome,
    ExecutorType,
    GateAlreadyDecided,
    GateAssignmentDto,
    GateInstanceDto,
    GateNotFound,
    GateReviewerIneligible,
    GateReviewerMismatch,
    GateState,
    GateType,
    InvalidRequirementCursor,
    InvalidRequirementInput,
    InvalidRequirementTransition,
    RecordState,
    RegisterSddBaselineResult,
    RepositoryBindingBlockedReason,
    RepositoryBindingConflict,
    RepositoryBindingRequestMissing,
    RepositoryState,
    RequirementDependencyUnavailable,
    RequirementDetailsDto,
    RequirementDto,
    RequirementError,
    RequirementNotFound,
    RequirementPage,
    RequirementState,
    RequirementType,
    SddBaselineDto,
    SddBaselineNotFound,
    StaleBaselineSubject,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    WorkItemDto,
    WorkItemNotFound,
    WorkItemState,
    derive_work_item_state,
    transition_requirement,
)
from control_plane.app.modules.requirement.ports import (
    ArtifactSnapshot,
    ArtifactState,
    ArtifactTrust,
    GatePolicySnapshot,
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


def record_repository_binding_blocked(
    db: Connection,
    *,
    work_item_id: str,
    repository_id: str,
    reason_code: RepositoryBindingBlockedReason,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDto:
    return _record_repository_binding_blocked(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        repository_id=repository_id,
        reason_code=reason_code,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def register_sdd_baseline(
    db: Connection,
    *,
    requirement_id: str,
    artifact_id: str,
    artifact_version: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> RegisterSddBaselineResult:
    return _register_sdd_baseline(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def submit_baseline_confirmation(
    db: Connection,
    *,
    requirement_id: str,
    sdd_baseline_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> BaselineConfirmationResult:
    return _submit_baseline_confirmation(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        sdd_baseline_id=sdd_baseline_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def decide_baseline(
    db: Connection,
    *,
    requirement_id: str,
    gate_id: str,
    outcome: DecisionOutcome,
    reason: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> BaselineDecisionResult:
    return _decide_baseline(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        gate_id=gate_id,
        outcome=outcome,
        reason=reason,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


__all__ = [
    "AssignmentState",
    "ArtifactSnapshot",
    "ArtifactState",
    "ArtifactTrust",
    "ArtifactUnavailable",
    "BaselineConfirmationResult",
    "BaselineDecisionResult",
    "CreateRequirementResult",
    "DecisionDto",
    "DecisionOutcome",
    "ExecutorType",
    "GateAlreadyDecided",
    "GateAssignmentDto",
    "GateInstanceDto",
    "GateNotFound",
    "GatePolicySnapshot",
    "GateReviewerIneligible",
    "GateReviewerMismatch",
    "GateState",
    "GateType",
    "InvalidRequirementCursor",
    "InvalidRequirementInput",
    "InvalidRequirementTransition",
    "RecordState",
    "RegisterSddBaselineResult",
    "RepositoryBindingConflict",
    "RepositoryBindingBlockedReason",
    "RepositoryBindingRequestMissing",
    "RepositoryState",
    "RequirementDependencies",
    "RequirementDto",
    "RequirementDetailsDto",
    "RequirementDependencyUnavailable",
    "RequirementError",
    "RequirementNotFound",
    "RequirementPage",
    "RequirementState",
    "RequirementType",
    "SddBaselineDto",
    "SddBaselineNotFound",
    "StaleBaselineSubject",
    "StaleRequirementRevision",
    "StaleWorkItemRevision",
    "WorkItemDto",
    "WorkItemNotFound",
    "WorkItemState",
    "create_requirement",
    "decide_baseline",
    "derive_work_item_state",
    "get_requirement",
    "list_requirements",
    "record_repository_binding",
    "record_repository_binding_blocked",
    "register_sdd_baseline",
    "start_requirement_preparation",
    "submit_baseline_confirmation",
    "transition_requirement",
]
