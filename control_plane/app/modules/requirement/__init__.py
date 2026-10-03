"""Public Requirement facade; other modules must not import internals."""

from datetime import datetime
from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.requirement.adapters.gate_policy_authorization import (
    RequirementPolicyAuthorization,
)
from control_plane.app.modules.requirement.adapters.gate_policy_runtime import (
    RequirementPolicyRuntime,
)
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
    acknowledge_evidence_request as _acknowledge_evidence_request,
)
from control_plane.app.modules.requirement.application import (
    acknowledge_formal_delivery_request as _acknowledge_formal_delivery_request,
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
    claim_evidence_requests as _claim_evidence_requests,
)
from control_plane.app.modules.requirement.application import (
    claim_formal_delivery_requests as _claim_formal_delivery_requests,
)
from control_plane.app.modules.requirement.application import (
    claim_integration_delivery_requests as _claim_integration_delivery_requests,
)
from control_plane.app.modules.requirement.application import (
    claim_repository_binding_requests as _claim_repository_binding_requests,
)
from control_plane.app.modules.requirement.application import (
    confirm_requirement_acceptance as _confirm_requirement_acceptance,
)
from control_plane.app.modules.requirement.application import (
    create_requirement as _create_requirement,
)
from control_plane.app.modules.requirement.application import (
    create_sdd_artifact as _create_sdd_artifact,
)
from control_plane.app.modules.requirement.application import decide_baseline as _decide_baseline
from control_plane.app.modules.requirement.application import (
    decide_formal_review as _decide_formal_review,
)
from control_plane.app.modules.requirement.application import (
    decide_requirement_acceptance as _decide_requirement_acceptance,
)
from control_plane.app.modules.requirement.application import (
    get_current_acceptance_proof as _get_current_acceptance_proof,
)
from control_plane.app.modules.requirement.application import (
    get_formal_delivery_admission as _get_formal_delivery_admission,
)
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
    get_requirement_delivery_snapshot as _get_requirement_delivery_snapshot,
)
from control_plane.app.modules.requirement.application import (
    get_sdd_artifact as _get_sdd_artifact,
)
from control_plane.app.modules.requirement.application import (
    list_requirements as _list_requirements,
)
from control_plane.app.modules.requirement.application import (
    reassign_baseline_gate as _reassign_baseline_gate,
)
from control_plane.app.modules.requirement.application import (
    record_external_merge_drift as _record_external_merge_drift,
)
from control_plane.app.modules.requirement.application import (
    record_formal_delivery_blocked as _record_formal_delivery_blocked,
)
from control_plane.app.modules.requirement.application import (
    record_formal_merged as _record_formal_merged,
)
from control_plane.app.modules.requirement.application import (
    record_formal_mr_ready as _record_formal_mr_ready,
)
from control_plane.app.modules.requirement.application import (
    record_formal_reconciliation_pending as _record_formal_reconciliation_pending,
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
    release_evidence_request as _release_evidence_request,
)
from control_plane.app.modules.requirement.application import (
    release_formal_delivery_request as _release_formal_delivery_request,
)
from control_plane.app.modules.requirement.application import (
    release_integration_delivery_request as _release_integration_delivery_request,
)
from control_plane.app.modules.requirement.application import (
    release_repository_binding_request as _release_repository_binding_request,
)
from control_plane.app.modules.requirement.application import (
    request_formal_merge as _request_formal_merge,
)
from control_plane.app.modules.requirement.application import (
    request_formal_merge_request as _request_formal_merge_request,
)
from control_plane.app.modules.requirement.application import (
    request_integration_baseline as _request_integration_baseline,
)
from control_plane.app.modules.requirement.application import (
    request_integration_merge as _request_integration_merge,
)
from control_plane.app.modules.requirement.application import (
    request_integration_merge_request as _request_integration_merge_request,
)
from control_plane.app.modules.requirement.application import (
    select_integration_baseline as _select_integration_baseline,
)
from control_plane.app.modules.requirement.application import (
    start_requirement_preparation as _start_requirement_preparation,
)
from control_plane.app.modules.requirement.application import start_work_item as _start_work_item
from control_plane.app.modules.requirement.application import (
    submit_baseline_confirmation as _submit_baseline_confirmation,
)
from control_plane.app.modules.requirement.application import (
    submit_external_validation as _submit_external_validation,
)
from control_plane.app.modules.requirement.application.delivery_assignments import (
    reassign_delivery_gate as _reassign_delivery_gate,
)
from control_plane.app.modules.requirement.domain import (
    AcceptanceConfirmationResult,
    AcceptanceDecisionResult,
    AcceptanceStale,
    AddWorkItemResult,
    ArtifactEvidenceReference,
    ArtifactUnavailable,
    AssignmentState,
    AssignWorkItemResult,
    BaselineConfirmationResult,
    BaselineDecisionResult,
    CreateRequirementResult,
    CreateSddArtifactResult,
    CurrentAcceptanceProof,
    DecisionDto,
    DecisionOutcome,
    DeliveryEvidenceConflict,
    DeliverySnapshotConflict,
    EvidenceUnavailableOrStale,
    ExecutorType,
    ExternalValidationRequestMessage,
    FormalDeliveryAdmission,
    FormalDeliveryBlocked,
    FormalDeliveryBlockedReason,
    FormalDeliveryBlockedResult,
    FormalDeliveryCommandResult,
    FormalDeliveryConflict,
    FormalDeliveryRequestMessage,
    FormalMergedResult,
    FormalMrReadyResult,
    FormalReviewStale,
    GateAlreadyDecided,
    GateAssignmentConflict,
    GateAssignmentDto,
    GateInstanceDto,
    GateNotFound,
    GateReassignmentResult,
    GateReviewerIneligible,
    GateReviewerMismatch,
    GateState,
    GateType,
    IntegrationBaselineRequestMessage,
    IntegrationBaselineSelectionDto,
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
    RequestIntegrationBaselineResult,
    RequirementDeliverySnapshot,
    RequirementDeliverySnapshotDto,
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
    SelectIntegrationBaselineResult,
    SelectionStale,
    StaleBaselineSubject,
    StaleGateRevision,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    SubmitExternalValidationResult,
    WorkItemAssigneeIneligible,
    WorkItemAssignmentConflict,
    WorkItemDto,
    WorkItemNotFound,
    WorkItemState,
    derive_work_item_state,
    transition_requirement,
)
from control_plane.app.modules.requirement.domain.acceptance import DeliveryGateReassignmentResult
from control_plane.app.modules.requirement.domain.gate_policy import ResolvedGatePolicy
from control_plane.app.modules.requirement.ports import (
    ArtifactSnapshot,
    ArtifactState,
    ArtifactTrust,
    DeliveryGatePolicySnapshot,
    DeliveryReviewerEligibilitySnapshot,
    GatePolicySnapshot,
    IntegrationBaselineEvidenceSnapshot,
    IntegrationBaselineEvidenceWorkItem,
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


def submit_external_validation(
    db: Connection,
    *,
    requirement_id: str,
    work_item_id: str,
    target_commit_sha: str,
    integration_merge_commit_sha: str,
    reference: str,
    notes: str,
    artifact_references: tuple[ArtifactEvidenceReference, ...],
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> SubmitExternalValidationResult:
    return _submit_external_validation(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        target_commit_sha=target_commit_sha,
        integration_merge_commit_sha=integration_merge_commit_sha,
        reference=reference,
        notes=notes,
        artifact_references=artifact_references,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def request_integration_baseline(
    db: Connection,
    *,
    requirement_id: str,
    expected_revision: int,
    expected_requirement_version: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> RequestIntegrationBaselineResult:
    return _request_integration_baseline(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        expected_revision=expected_revision,
        expected_requirement_version=expected_requirement_version,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def select_integration_baseline(
    db: Connection,
    *,
    requirement_id: str,
    delivery_snapshot_id: str,
    integration_baseline_id: str,
    expected_revision: int,
    expected_requirement_version: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> SelectIntegrationBaselineResult:
    return _select_integration_baseline(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        delivery_snapshot_id=delivery_snapshot_id,
        integration_baseline_id=integration_baseline_id,
        expected_revision=expected_revision,
        expected_requirement_version=expected_requirement_version,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def confirm_requirement_acceptance(
    db: Connection,
    *,
    requirement_id: str,
    selection_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> AcceptanceConfirmationResult:
    return _confirm_requirement_acceptance(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        selection_id=selection_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def decide_requirement_acceptance(
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
) -> AcceptanceDecisionResult:
    return _decide_requirement_acceptance(
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


def get_current_acceptance_proof(
    db: Connection,
    *,
    requirement_id: str,
    dependencies: RequirementDependencies,
) -> CurrentAcceptanceProof:
    return _get_current_acceptance_proof(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
    )


def request_formal_merge_request(
    db: Connection,
    *,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> FormalDeliveryCommandResult:
    return _request_formal_merge_request(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def request_formal_merge(
    db: Connection,
    *,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> FormalDeliveryCommandResult:
    return _request_formal_merge(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )


def decide_formal_review(
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
) -> AcceptanceDecisionResult:
    return _decide_formal_review(
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


def get_formal_delivery_admission(
    db: Connection,
    *,
    work_item_id: str,
    dependencies: RequirementDependencies,
) -> FormalDeliveryAdmission:
    return _get_formal_delivery_admission(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        dependencies=dependencies,
    )


def record_formal_mr_ready(
    db: Connection,
    *,
    work_item_id: str,
    binding_id: str,
    head_sha: str,
    expected_revision: int,
    assignment: DeliveryGatePolicySnapshot,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> FormalMrReadyResult:
    return _record_formal_mr_ready(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        binding_id=binding_id,
        head_sha=head_sha,
        expected_revision=expected_revision,
        assignment=assignment,
        actor=actor,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        dependencies=dependencies,
    )


def record_formal_delivery_blocked(
    db: Connection,
    *,
    work_item_id: str,
    binding_id: str | None,
    reason_code: FormalDeliveryBlockedReason,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> FormalDeliveryBlockedResult:
    return _record_formal_delivery_blocked(
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


def record_formal_merged(
    db: Connection,
    *,
    work_item_id: str,
    binding_id: str,
    head_sha: str,
    merge_commit_sha: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> FormalMergedResult:
    return _record_formal_merged(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        binding_id=binding_id,
        head_sha=head_sha,
        merge_commit_sha=merge_commit_sha,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
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


def claim_evidence_requests(
    db: Connection,
    *,
    limit: int,
    available_before: datetime,
    lease_until: datetime,
    dependencies: RequirementDependencies,
) -> tuple[ExternalValidationRequestMessage | IntegrationBaselineRequestMessage, ...]:
    return _claim_evidence_requests(
        dependencies.repository_factory(db),
        limit=limit,
        available_before=available_before,
        lease_until=lease_until,
    )


def claim_formal_delivery_requests(
    db: Connection,
    *,
    limit: int,
    available_before: datetime,
    lease_until: datetime,
    dependencies: RequirementDependencies,
) -> tuple[FormalDeliveryRequestMessage, ...]:
    return _claim_formal_delivery_requests(
        dependencies.repository_factory(db),
        limit=limit,
        available_before=available_before,
        lease_until=lease_until,
    )


def acknowledge_formal_delivery_request(
    db: Connection,
    *,
    message_id: str,
    dependencies: RequirementDependencies,
) -> None:
    _acknowledge_formal_delivery_request(
        dependencies.repository_factory(db),
        message_id=message_id,
        dependencies=dependencies,
    )


def release_formal_delivery_request(
    db: Connection,
    *,
    message_id: str,
    error_code: str,
    available_at: datetime,
    dependencies: RequirementDependencies,
) -> None:
    _release_formal_delivery_request(
        dependencies.repository_factory(db),
        message_id=message_id,
        error_code=error_code,
        available_at=available_at,
    )


def acknowledge_evidence_request(
    db: Connection,
    *,
    message_id: str,
    consumer: str,
    dependencies: RequirementDependencies,
) -> None:
    _acknowledge_evidence_request(
        dependencies.repository_factory(db),
        message_id=message_id,
        consumer=consumer,
        dependencies=dependencies,
    )


def release_evidence_request(
    db: Connection,
    *,
    message_id: str,
    error_code: str,
    available_at: datetime,
    dependencies: RequirementDependencies,
) -> None:
    _release_evidence_request(
        dependencies.repository_factory(db),
        message_id=message_id,
        error_code=error_code,
        available_at=available_at,
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


def get_requirement_for_update(
    db: Connection,
    *,
    requirement_id: str,
    dependencies: RequirementDependencies,
) -> RequirementDetailsDto:
    """Hold the parent write lock until the caller's transaction completes."""
    return _get_requirement(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        for_update=True,
    )


def get_requirement_delivery_snapshot(
    db: Connection,
    *,
    requirement_id: str,
    dependencies: RequirementDependencies,
) -> RequirementDeliverySnapshotDto:
    """Return the current immutable delivery input through the package facade."""
    return _get_requirement_delivery_snapshot(
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


def reassign_baseline_gate(
    db: Connection,
    *,
    requirement_id: str,
    gate_id: str,
    reviewer_id: str,
    reason: str,
    expected_gate_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> GateReassignmentResult:
    return _reassign_baseline_gate(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        gate_id=gate_id,
        reviewer_id=reviewer_id,
        reason=reason,
        expected_gate_revision=expected_gate_revision,
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
    "DeliveryGateReassignmentResult",
    "reassign_delivery_gate",
    "RequirementPolicyRuntime",
    "RequirementPolicyAuthorization",
    "ResolvedGatePolicy",
    "AcceptanceConfirmationResult",
    "AcceptanceDecisionResult",
    "AcceptanceStale",
    "AssignmentState",
    "AddWorkItemResult",
    "AssignWorkItemResult",
    "ArtifactEvidenceReference",
    "ArtifactSnapshot",
    "ArtifactState",
    "ArtifactTrust",
    "ArtifactUnavailable",
    "BaselineConfirmationResult",
    "BaselineDecisionResult",
    "CreateRequirementResult",
    "CreateSddArtifactResult",
    "CurrentAcceptanceProof",
    "DecisionDto",
    "DecisionOutcome",
    "DeliveryEvidenceConflict",
    "DeliverySnapshotConflict",
    "DeliveryGatePolicySnapshot",
    "DeliveryReviewerEligibilitySnapshot",
    "ExecutorType",
    "EvidenceUnavailableOrStale",
    "ExternalValidationRequestMessage",
    "FormalDeliveryAdmission",
    "FormalDeliveryBlocked",
    "FormalDeliveryBlockedReason",
    "FormalDeliveryBlockedResult",
    "FormalDeliveryCommandResult",
    "FormalDeliveryConflict",
    "FormalDeliveryRequestMessage",
    "FormalMergedResult",
    "FormalMrReadyResult",
    "FormalReviewStale",
    "GateAlreadyDecided",
    "GateAssignmentDto",
    "GateAssignmentConflict",
    "GateReassignmentResult",
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
    "IntegrationBaselineEvidenceSnapshot",
    "IntegrationBaselineEvidenceWorkItem",
    "IntegrationBaselineRequestMessage",
    "IntegrationBaselineSelectionDto",
    "RecordState",
    "RegisterSddBaselineResult",
    "RepositoryBindingConflict",
    "RepositoryBindingMessageInvalid",
    "RepositoryBindingContext",
    "RepositoryBindingBlockedReason",
    "RepositoryBindingRequestMissing",
    "RepositoryState",
    "RequirementDependencies",
    "RequirementDeliverySnapshot",
    "RequirementDto",
    "RequirementDetailsDto",
    "RequirementDeliverySnapshotDto",
    "RequirementDependencyUnavailable",
    "RequirementError",
    "RequirementNotFound",
    "RequirementPage",
    "RequirementState",
    "RequirementType",
    "RequestIntegrationBaselineResult",
    "SelectIntegrationBaselineResult",
    "SelectionStale",
    "SddBaselineDto",
    "SddArtifactNotFound",
    "SddArtifactVersionDto",
    "SddBaselineNotFound",
    "StaleBaselineSubject",
    "StaleGateRevision",
    "StaleRequirementRevision",
    "StaleWorkItemRevision",
    "SubmitExternalValidationResult",
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
    "acknowledge_evidence_request",
    "acknowledge_formal_delivery_request",
    "add_work_item",
    "assign_work_item",
    "acknowledge_integration_delivery_request",
    "claim_integration_delivery_requests",
    "claim_evidence_requests",
    "claim_formal_delivery_requests",
    "claim_repository_binding_requests",
    "confirm_requirement_acceptance",
    "create_requirement",
    "create_sdd_artifact",
    "decide_baseline",
    "decide_requirement_acceptance",
    "decide_formal_review",
    "derive_work_item_state",
    "get_requirement",
    "get_requirement_for_update",
    "get_requirement_delivery_snapshot",
    "get_sdd_artifact",
    "get_integration_delivery_context",
    "get_current_acceptance_proof",
    "get_formal_delivery_admission",
    "get_repository_binding_context",
    "list_requirements",
    "record_repository_binding",
    "record_repository_binding_blocked",
    "reassign_baseline_gate",
    "record_external_merge_drift",
    "record_formal_merged",
    "record_formal_delivery_blocked",
    "record_formal_mr_ready",
    "record_formal_reconciliation_pending",
    "record_integration_delivery_blocked",
    "record_integration_merged",
    "record_integration_mr_ready",
    "record_integration_reconciliation_pending",
    "request_integration_baseline",
    "request_formal_merge",
    "request_formal_merge_request",
    "request_integration_merge",
    "request_integration_merge_request",
    "select_integration_baseline",
    "release_repository_binding_request",
    "release_evidence_request",
    "release_formal_delivery_request",
    "release_integration_delivery_request",
    "register_sdd_baseline",
    "start_requirement_preparation",
    "start_work_item",
    "submit_baseline_confirmation",
    "submit_external_validation",
    "transition_requirement",
]


def record_formal_reconciliation_pending(
    db: Connection,
    *,
    work_item_id: str,
    binding_id: str | None,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> None:
    _record_formal_reconciliation_pending(
        dependencies.repository_factory(db),
        work_item_id=work_item_id,
        binding_id=binding_id,
        expected_revision=expected_revision,
        actor=actor,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        dependencies=dependencies,
    )


def reassign_delivery_gate(
    db: Connection,
    *,
    requirement_id: str,
    gate_id: str,
    candidate_id: str,
    expected_gate_revision: int,
    reason: str,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> DeliveryGateReassignmentResult:
    return _reassign_delivery_gate(
        dependencies.repository_factory(db),
        requirement_id=requirement_id,
        gate_id=gate_id,
        candidate_id=candidate_id,
        expected_gate_revision=expected_gate_revision,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )
