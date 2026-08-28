"""Public Requirement facade; other modules must not import internals."""

from datetime import datetime
from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.requirement.application import (
    IntegrationDeliveryMessageInvalid,
    IntegrationDeliveryRequestMissing,
    RequirementDependencies,
    WorkItemActorDenied,
    WorkItemDeliveryConflict,
    WorkItemDeliveryDto,
    WorkItemDeliveryResult,
)
from control_plane.app.modules.requirement.application import (
    acknowledge_integration_delivery_request as _acknowledge_integration_delivery_request,
)
from control_plane.app.modules.requirement.application import (
    acknowledge_repository_binding_request as _acknowledge_repository_binding_request,
)
from control_plane.app.modules.requirement.application import add_work_item as _add_work_item
from control_plane.app.modules.requirement.application import (
    assign_work_item as _assign_work_item,
)
from control_plane.app.modules.requirement.application import (
    claim_integration_delivery_requests as _claim_integration_delivery_requests,
)
from control_plane.app.modules.requirement.application import (
    claim_repository_binding_requests as _claim_repository_binding_requests,
)
from control_plane.app.modules.requirement.application import (
    create_requirement as _create_requirement,
)
from control_plane.app.modules.requirement.application import (
    create_sdd_artifact as _create_sdd_artifact,
)
from control_plane.app.modules.requirement.application import decide_baseline as _decide_baseline
from control_plane.app.modules.requirement.application import (
    get_integration_delivery_context as _get_integration_delivery_context,
)
from control_plane.app.modules.requirement.application import (
    get_repository_binding_context as _get_repository_binding_context,
)
from control_plane.app.modules.requirement.application import (
    get_requirement as _get_requirement,
)
from control_plane.app.modules.requirement.application import (
    get_sdd_artifact as _get_sdd_artifact,
)
from control_plane.app.modules.requirement.application import (
    list_requirements as _list_requirements,
)
from control_plane.app.modules.requirement.application import (
    record_external_merge_drift as _record_external_merge_drift,
)
from control_plane.app.modules.requirement.application import (
    record_integration_delivery_blocked as _record_integration_delivery_blocked,
)
from control_plane.app.modules.requirement.application import (
    record_integration_merged as _record_integration_merged,
)
from control_plane.app.modules.requirement.application import (
    record_integration_mr_ready as _record_integration_mr_ready,
)
from control_plane.app.modules.requirement.application import (
    record_integration_reconciliation_pending as _record_integration_reconciliation_pending,
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
    release_integration_delivery_request as _release_integration_delivery_request,
)
from control_plane.app.modules.requirement.application import (
    release_repository_binding_request as _release_repository_binding_request,
)
from control_plane.app.modules.requirement.application import (
    request_integration_merge as _request_integration_merge,
)
from control_plane.app.modules.requirement.application import (
    request_integration_merge_request as _request_integration_merge_request,
)
from control_plane.app.modules.requirement.application import (
    start_requirement_preparation as _start_requirement_preparation,
)
from control_plane.app.modules.requirement.application import start_work_item as _start_work_item
from control_plane.app.modules.requirement.application import (
    submit_baseline_confirmation as _submit_baseline_confirmation,
)
from control_plane.app.modules.requirement.domain import (
    AddWorkItemResult,
    ArtifactUnavailable,
    AssignmentState,
    AssignWorkItemResult,
    BaselineConfirmationResult,
    BaselineDecisionResult,
    CreateRequirementResult,
    CreateSddArtifactResult,
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
    IntegrationDeliveryBlockedReason,
    IntegrationDeliveryContext,
    IntegrationDeliveryRequestKind,
    IntegrationDeliveryRequestMessage,
    IntegrationDeliveryState,
    InvalidRequirementCursor,
    InvalidRequirementInput,
    InvalidRequirementTransition,
    RecordState,
    RegisterSddBaselineResult,
    RepositoryBindingBlockedReason,
    RepositoryBindingConflict,
    RepositoryBindingContext,
    RepositoryBindingMessageInvalid,
    RepositoryBindingRequestMessage,
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
    SddArtifactNotFound,
    SddArtifactVersionDto,
    SddBaselineDto,
    SddBaselineNotFound,
    StaleBaselineSubject,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    WorkItemAssigneeIneligible,
    WorkItemAssignmentConflict,
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


def add_work_item(
    db: Connection,
    *,
    requirement_id: str,
    repository_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> AddWorkItemResult:
    return _add_work_item(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        repository_id=repository_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def assign_work_item(
    db: Connection,
    *,
    requirement_id: str,
    work_item_id: str,
    human_owner_id: str,
    reason: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> AssignWorkItemResult:
    return _assign_work_item(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        human_owner_id=human_owner_id,
        reason=reason,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def create_sdd_artifact(
    db: Connection,
    *,
    requirement_id: str,
    artifact_id: str | None,
    content: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> CreateSddArtifactResult:
    return _create_sdd_artifact(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        artifact_id=artifact_id,
        content=content,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def get_sdd_artifact(
    db: Connection,
    *,
    requirement_id: str,
    artifact_id: str,
    artifact_version: int,
    dependencies: RequirementDependencies,
) -> SddArtifactVersionDto:
    return _get_sdd_artifact(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
    )


def claim_repository_binding_requests(
    db: Connection,
    *,
    limit: int,
    available_before: datetime,
    lease_until: datetime,
    dependencies: RequirementDependencies,
) -> tuple[RepositoryBindingRequestMessage, ...]:
    return _claim_repository_binding_requests(
        dependencies.repository_factory(db),
        limit=limit,
        available_before=available_before,
        lease_until=lease_until,
    )


def claim_integration_delivery_requests(
    db: Connection,
    *,
    limit: int,
    available_before: datetime,
    lease_until: datetime,
    dependencies: RequirementDependencies,
) -> tuple[IntegrationDeliveryRequestMessage, ...]:
    return _claim_integration_delivery_requests(
        dependencies.repository_factory(db),
        limit=limit,
        available_before=available_before,
        lease_until=lease_until,
        dependencies=dependencies,
    )


def acknowledge_integration_delivery_request(
    db: Connection,
    *,
    message_id: str,
    consumer: str,
    dependencies: RequirementDependencies,
) -> None:
    _acknowledge_integration_delivery_request(
        dependencies.repository_factory(db),
        message_id=message_id,
        consumer=consumer,
        dependencies=dependencies,
    )


def release_integration_delivery_request(
    db: Connection,
    *,
    message_id: str,
    error_code: str,
    available_at: datetime,
    dependencies: RequirementDependencies,
) -> None:
    _release_integration_delivery_request(
        dependencies.repository_factory(db),
        message_id=message_id,
        error_code=error_code,
        available_at=available_at,
        dependencies=dependencies,
    )


def acknowledge_repository_binding_request(
    db: Connection,
    *,
    message_id: str,
    consumer: str,
    dependencies: RequirementDependencies,
) -> RequirementDto:
    return _acknowledge_repository_binding_request(
        dependencies.repository_factory(db),
        message_id=message_id,
        consumer=consumer,
        dependencies=dependencies,
    )


def release_repository_binding_request(
    db: Connection,
    *,
    message_id: str,
    error_code: str,
    available_at: datetime,
    dependencies: RequirementDependencies,
) -> None:
    _release_repository_binding_request(
        dependencies.repository_factory(db),
        message_id=message_id,
        error_code=error_code,
        available_at=available_at,
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


def start_work_item(
    db: Connection,
    *,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _start_work_item(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def request_integration_merge_request(
    db: Connection,
    *,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _request_integration_merge_request(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def request_integration_merge(
    db: Connection,
    *,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _request_integration_merge(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        work_item_id=work_item_id,
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


def get_repository_binding_context(
    db: Connection,
    *,
    work_item_id: str,
    dependencies: RequirementDependencies,
) -> RepositoryBindingContext:
    return _get_repository_binding_context(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
    )


def get_integration_delivery_context(
    db: Connection,
    *,
    work_item_id: str,
    dependencies: RequirementDependencies,
) -> IntegrationDeliveryContext:
    return _get_integration_delivery_context(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
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
    correlation_id: str,
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
        correlation_id=correlation_id,
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
    correlation_id: str,
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
        correlation_id=correlation_id,
        dependencies=dependencies,
    )


def record_integration_mr_ready(
    db: Connection,
    *,
    work_item_id: str,
    binding_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _record_integration_mr_ready(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        binding_id=binding_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        dependencies=dependencies,
    )


def record_integration_delivery_blocked(
    db: Connection,
    *,
    work_item_id: str,
    binding_id: str | None,
    reason_code: IntegrationDeliveryBlockedReason,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _record_integration_delivery_blocked(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        binding_id=binding_id,
        reason_code=reason_code,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        dependencies=dependencies,
    )


def record_integration_reconciliation_pending(
    db: Connection,
    *,
    work_item_id: str,
    binding_id: str | None,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _record_integration_reconciliation_pending(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        binding_id=binding_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        dependencies=dependencies,
    )


def record_integration_merged(
    db: Connection,
    *,
    work_item_id: str,
    binding_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _record_integration_merged(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        binding_id=binding_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        dependencies=dependencies,
    )


def record_external_merge_drift(
    db: Connection,
    *,
    work_item_id: str,
    binding_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> WorkItemDeliveryResult:
    return _record_external_merge_drift(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        binding_id=binding_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
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
    "AddWorkItemResult",
    "AssignWorkItemResult",
    "ArtifactSnapshot",
    "ArtifactState",
    "ArtifactTrust",
    "ArtifactUnavailable",
    "BaselineConfirmationResult",
    "BaselineDecisionResult",
    "CreateRequirementResult",
    "CreateSddArtifactResult",
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
    "IntegrationDeliveryBlockedReason",
    "IntegrationDeliveryContext",
    "IntegrationDeliveryMessageInvalid",
    "IntegrationDeliveryRequestKind",
    "IntegrationDeliveryRequestMessage",
    "IntegrationDeliveryRequestMissing",
    "IntegrationDeliveryState",
    "RecordState",
    "RegisterSddBaselineResult",
    "RepositoryBindingConflict",
    "RepositoryBindingMessageInvalid",
    "RepositoryBindingContext",
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
    "SddArtifactNotFound",
    "SddArtifactVersionDto",
    "SddBaselineNotFound",
    "StaleBaselineSubject",
    "StaleRequirementRevision",
    "StaleWorkItemRevision",
    "WorkItemDto",
    "WorkItemAssigneeIneligible",
    "WorkItemAssignmentConflict",
    "WorkItemActorDenied",
    "WorkItemDeliveryConflict",
    "WorkItemDeliveryDto",
    "WorkItemDeliveryResult",
    "WorkItemNotFound",
    "WorkItemState",
    "RepositoryBindingRequestMessage",
    "acknowledge_repository_binding_request",
    "add_work_item",
    "assign_work_item",
    "acknowledge_integration_delivery_request",
    "claim_integration_delivery_requests",
    "claim_repository_binding_requests",
    "create_requirement",
    "create_sdd_artifact",
    "decide_baseline",
    "derive_work_item_state",
    "get_requirement",
    "get_sdd_artifact",
    "get_integration_delivery_context",
    "get_repository_binding_context",
    "list_requirements",
    "record_repository_binding",
    "record_repository_binding_blocked",
    "record_external_merge_drift",
    "record_integration_delivery_blocked",
    "record_integration_merged",
    "record_integration_mr_ready",
    "record_integration_reconciliation_pending",
    "request_integration_merge",
    "request_integration_merge_request",
    "release_repository_binding_request",
    "release_integration_delivery_request",
    "register_sdd_baseline",
    "start_requirement_preparation",
    "start_work_item",
    "submit_baseline_confirmation",
    "transition_requirement",
]
