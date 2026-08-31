from typing import Any

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    AssignmentState,
    DecisionDto,
    DecisionOutcome,
    ExecutorType,
    GateAssignmentDto,
    GateInstanceDto,
    GateState,
    GateType,
    IntegrationDeliveryBlockedReason,
    IntegrationDeliveryState,
    InvalidRequirementInput,
    RecordState,
    RepositoryBindingBlockedReason,
    RepositoryState,
    RequirementDependencyUnavailable,
    RequirementDto,
    RequirementState,
    RequirementType,
    SddArtifactVersionDto,
    SddBaselineDto,
    WorkItemAssignmentDto,
    WorkItemDto,
    WorkItemState,
    canonical_route_snapshot_hash,
)
from control_plane.app.modules.requirement.ports import RequirementRepository
from control_plane.app.shared.api.request_id import current_request_id


def actor_id(actor: Any) -> str:
    value = getattr(actor, "account_id", None) or getattr(actor, "employee_id", None)
    if not isinstance(value, str) or not value:
        raise ValueError("Requirement actor requires a stable identifier")
    return value


def validated_correlation_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequirementInput("correlation ID is invalid")
    return value


def validate_frozen_route_snapshot(
    snapshot: object,
    *,
    expected_hash: object,
    expected_version: object,
    expected_requirement_type: object,
) -> dict[str, object]:
    if not isinstance(snapshot, dict):
        raise RequirementDependencyUnavailable("Frozen Route Snapshot is invalid")
    if (
        snapshot.get("version") != expected_version
        or snapshot.get("requirementType") != expected_requirement_type
    ):
        raise RequirementDependencyUnavailable("Frozen Route Snapshot subject is invalid")
    try:
        actual_hash = canonical_route_snapshot_hash(snapshot)
    except (TypeError, ValueError) as error:
        raise RequirementDependencyUnavailable("Frozen Route Snapshot is invalid") from error
    if not isinstance(expected_hash, str) or actual_hash != expected_hash:
        raise RequirementDependencyUnavailable("Frozen Route Snapshot hash is invalid")
    return snapshot


def requirement_dto(row: Any) -> RequirementDto:
    return RequirementDto(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        type=RequirementType(row["type"]),
        title=row["title"],
        description=row["description"],
        acceptance_criteria=tuple(row["acceptance_criteria"]),
        created_by=row["created_by"],
        initial_repository_id=row["initial_repository_id"],
        route_snapshot_version=row["route_snapshot_version"],
        route_snapshot_hash=row["route_snapshot_hash"],
        route_snapshot=dict(row["route_snapshot"]),
        state=RequirementState(row["state"]),
        record_state=RecordState(row["record_state"]),
        requirement_version=row["requirement_version"],
        required_work_item_set_version=row["required_work_item_set_version"],
        required_work_item_set_hash=row["required_work_item_set_hash"],
        current_sdd_baseline_id=(
            None if row["current_sdd_baseline_id"] is None else str(row["current_sdd_baseline_id"])
        ),
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def work_item_dto(row: Any) -> WorkItemDto:
    return WorkItemDto(
        id=str(row["id"]),
        requirement_id=str(row["requirement_id"]),
        created_by=row["created_by"],
        human_owner_id=row["human_owner_id"],
        executor_type=ExecutorType(row["executor_type"]),
        executor_id=row["executor_id"],
        required_capabilities=tuple(row["required_capabilities"]),
        assignment_state=AssignmentState(row["assignment_state"]),
        repository_state=RepositoryState(row["repository_state"]),
        state=WorkItemState(row["state"]),
        repository_id=row["repository_id"],
        base_commit_sha=row["base_commit_sha"],
        task_branch=row["task_branch"],
        repository_blocked_reason_code=(
            None
            if row["repository_blocked_reason_code"] is None
            else RepositoryBindingBlockedReason(row["repository_blocked_reason_code"])
        ),
        repository_blocked_at=row["repository_blocked_at"],
        integration_delivery_state=IntegrationDeliveryState(row["integration_delivery_state"]),
        integration_merge_request_binding_id=(
            None
            if row["integration_merge_request_binding_id"] is None
            else str(row["integration_merge_request_binding_id"])
        ),
        integration_blocked_reason_code=(
            None
            if row["integration_blocked_reason_code"] is None
            else IntegrationDeliveryBlockedReason(row["integration_blocked_reason_code"])
        ),
        integration_updated_at=row["integration_updated_at"],
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def work_item_assignment_dto(row: Any) -> WorkItemAssignmentDto:
    return WorkItemAssignmentDto(
        id=str(row["id"]),
        work_item_id=str(row["work_item_id"]),
        assignee_id=row["assignee_id"],
        assigned_by=row["assigned_by"],
        reason=row["reason"],
        revision=row["revision"],
        assigned_at=row["assigned_at"],
        superseded_at=row["superseded_at"],
    )


def sdd_artifact_version_dto(row: Any) -> SddArtifactVersionDto:
    return SddArtifactVersionDto(
        artifact_id=str(row["artifact_id"]),
        version=row["version"],
        requirement_id=str(row["requirement_id"]),
        sha256=row["sha256"],
        state=row["state"],
        media_type=row["media_type"],
        trust=row["trust"],
        content=row["content"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def sdd_baseline_dto(row: Any) -> SddBaselineDto:
    return SddBaselineDto(
        id=str(row["id"]),
        requirement_id=str(row["requirement_id"]),
        requirement_version=row["requirement_version"],
        artifact_id=row["artifact_id"],
        artifact_version=row["artifact_version"],
        artifact_hash=row["artifact_hash"],
        route_snapshot_version=row["route_snapshot_version"],
        route_snapshot_hash=row["route_snapshot_hash"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def gate_instance_dto(row: Any) -> GateInstanceDto:
    return GateInstanceDto(
        id=str(row["id"]),
        gate_type=GateType(row["gate_type"]),
        requirement_id=str(row["requirement_id"]),
        requirement_version=row["requirement_version"],
        sdd_baseline_id=str(row["sdd_baseline_id"]),
        artifact_id=row["artifact_id"],
        artifact_version=row["artifact_version"],
        artifact_hash=row["artifact_hash"],
        route_snapshot_version=row["route_snapshot_version"],
        route_snapshot_hash=row["route_snapshot_hash"],
        policy_code=row["policy_code"],
        policy_version=row["policy_version"],
        policy_snapshot_hash=row["policy_snapshot_hash"],
        state=GateState(row["state"]),
        revision=row["revision"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


def gate_assignment_dto(row: Any) -> GateAssignmentDto:
    return GateAssignmentDto(
        id=str(row["id"]),
        gate_instance_id=str(row["gate_instance_id"]),
        default_reviewer_id=row["default_reviewer_id"],
        current_reviewer_id=row["current_reviewer_id"],
        revision=row["revision"],
        assigned_at=row["assigned_at"],
        superseded_at=row["superseded_at"],
    )


def decision_dto(row: Any) -> DecisionDto:
    return DecisionDto(
        id=str(row["id"]),
        gate_instance_id=str(row["gate_instance_id"]),
        gate_assignment_id=str(row["gate_assignment_id"]),
        reviewer_id=row["reviewer_id"],
        outcome=DecisionOutcome(row["outcome"]),
        reason=row["reason"],
        subject_revision=row["subject_revision"],
        decided_at=row["decided_at"],
    )


def audit(
    repository: RequirementRepository,
    *,
    dependencies: RequirementDependencies,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    correlation_id: str | None = None,
) -> None:
    now = dependencies.clock.now()
    stable_correlation_id = (
        current_request_id() or str(dependencies.random.uuid4())
        if correlation_id is None
        else validated_correlation_id(correlation_id)
    )
    record_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=now,
            actor=actor,
            actor_type="SYSTEM" if actor == "SYSTEM" else "HUMAN",
            action=action,
            target_type=target_type,
            target_id=target_id,
            result="SUCCESS",
            reason=reason,
            correlation_id=stable_correlation_id,
        ),
        dependencies.audit,
    )
