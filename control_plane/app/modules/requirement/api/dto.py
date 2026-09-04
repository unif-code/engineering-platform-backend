from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field

from control_plane.app.modules.requirement.application.delivery import (
    WorkItemDeliveryDto,
    WorkItemDeliveryResult,
)
from control_plane.app.modules.requirement.domain import (
    AcceptanceConfirmationResult,
    AcceptanceDecisionResult,
    AddWorkItemResult,
    AssignmentState,
    AssignWorkItemResult,
    BaselineConfirmationResult,
    BaselineDecisionResult,
    CreateRequirementResult,
    CreateSddArtifactResult,
    DecisionDto,
    DecisionOutcome,
    DeliveryDecisionDto,
    DeliveryGateAssignmentDto,
    DeliveryGateDto,
    DeliveryGateState,
    DeliveryGateType,
    ExecutorType,
    ExternalValidationSubmission,
    FormalDeliveryCommandResult,
    FormalDeliveryState,
    GateAssignmentDto,
    GateInstanceDto,
    GateReassignmentResult,
    GateState,
    GateType,
    IntegrationBaselineSelectionDto,
    IntegrationDeliveryBlockedReason,
    IntegrationDeliveryState,
    RecordState,
    RegisterSddBaselineResult,
    RepositoryBindingBlockedReason,
    RepositoryState,
    RequestIntegrationBaselineResult,
    RequirementDeliverySnapshot,
    RequirementDetailsDto,
    RequirementDto,
    RequirementPage,
    RequirementType,
    SddArtifactVersionDto,
    SddBaselineDto,
    SelectIntegrationBaselineResult,
    SubmitExternalValidationResult,
    WorkItemAssignmentDto,
    WorkItemDto,
)
from control_plane.app.shared.api.camel import CamelModel


class StrictCamelModel(CamelModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=False,
        validate_by_alias=True,
        validate_by_name=False,
    )


class RequirementState(StrEnum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    AWAITING_ACCEPTANCE = "AWAITING_ACCEPTANCE"
    AWAITING_MERGE = "AWAITING_MERGE"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


class WorkItemState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    AWAITING_MERGE = "AWAITING_MERGE"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


class CreateRequirementRequestDto(StrictCamelModel):
    workspace_id: UUID
    type: RequirementType
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10000)
    acceptance_criteria: list[str] = Field(min_length=1)
    initial_repository_id: str = Field(min_length=1)


class RegisterSddBaselineRequestDto(StrictCamelModel):
    artifact_id: UUID
    artifact_version: int = Field(strict=True, ge=1)


class CreateSddArtifactRequestDto(StrictCamelModel):
    artifact_id: UUID | None = None
    content: str = Field(min_length=1, max_length=200_000)


class AddWorkItemRequestDto(StrictCamelModel):
    repository_id: str = Field(min_length=1, max_length=200)


class AssignWorkItemRequestDto(StrictCamelModel):
    human_owner_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class ReassignBaselineGateRequestDto(StrictCamelModel):
    reviewer_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class SubmitBaselineConfirmationRequestDto(StrictCamelModel):
    sdd_baseline_id: UUID


class DecideBaselineRequestDto(StrictCamelModel):
    gate_id: UUID
    outcome: DecisionOutcome
    reason: str = Field(min_length=1, max_length=2000)


class WorkItemDeliveryCommandRequestDto(StrictCamelModel):
    pass


class ArtifactEvidenceReferenceRequestDto(StrictCamelModel):
    artifact_id: str = Field(min_length=1, max_length=200)
    artifact_version: str = Field(min_length=1, max_length=200)
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SubmitExternalValidationRequestDto(StrictCamelModel):
    target_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    integration_merge_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    reference: str = Field(min_length=1, max_length=2000)
    notes: str = Field(min_length=1, max_length=10000)
    artifact_references: list[ArtifactEvidenceReferenceRequestDto] = Field(min_length=1)


class RequestIntegrationBaselineRequestDto(StrictCamelModel):
    expected_requirement_version: int = Field(strict=True, ge=1)


class SelectIntegrationBaselineRequestDto(RequestIntegrationBaselineRequestDto):
    delivery_snapshot_id: UUID
    integration_baseline_id: UUID


class AcceptanceConfirmationRequestDto(StrictCamelModel):
    selection_id: UUID


class DeliveryDecisionRequestDto(StrictCamelModel):
    gate_id: UUID
    outcome: DecisionOutcome
    reason: str = Field(min_length=1, max_length=2000)


class FormalDeliveryCommandRequestDto(StrictCamelModel):
    pass


class RequirementResponseDto(CamelModel):
    id: UUID
    workspace_id: UUID
    type: RequirementType
    title: str
    description: str
    acceptance_criteria: list[str]
    acceptance_criteria_version: int
    acceptance_criteria_hash: str
    created_by: str
    initial_repository_id: str
    route_snapshot_version: int
    route_snapshot_hash: str
    route_snapshot: dict[str, object]
    state: RequirementState
    record_state: RecordState
    requirement_version: int
    required_work_item_set_version: int
    required_work_item_set_hash: str
    current_sdd_baseline_id: UUID | None
    current_integration_baseline_selection_id: UUID | None
    current_acceptance_gate_id: UUID | None
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: RequirementDto) -> "RequirementResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class WorkItemResponseDto(CamelModel):
    id: UUID
    requirement_id: UUID
    created_by: str
    human_owner_id: str | None
    executor_type: ExecutorType
    executor_id: str | None
    required_capabilities: list[str]
    assignment_state: AssignmentState
    repository_state: RepositoryState
    state: WorkItemState
    repository_id: str
    base_commit_sha: str | None
    task_branch: str | None
    repository_blocked_reason_code: RepositoryBindingBlockedReason | None
    repository_blocked_at: datetime | None
    integration_delivery_state: IntegrationDeliveryState
    integration_merge_request_binding_id: UUID | None
    integration_blocked_reason_code: IntegrationDeliveryBlockedReason | None
    integration_updated_at: datetime | None
    formal_delivery_state: FormalDeliveryState
    formal_merge_request_binding_id: UUID | None
    formal_blocked_reason_code: str | None
    formal_updated_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: WorkItemDto) -> "WorkItemResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class ExternalValidationSubmissionResponseDto(CamelModel):
    message_id: UUID
    requirement_id: UUID
    requirement_version: int
    work_item_id: UUID
    work_item_revision: int
    repository_id: str
    integration_merge_request_binding_id: UUID
    target_commit_sha: str
    integration_merge_commit_sha: str
    reference: str
    notes: str
    artifact_references: list["ArtifactEvidenceReferenceResponseDto"]
    submitted_by: str
    submitted_at: datetime

    @classmethod
    def from_domain(
        cls,
        value: ExternalValidationSubmission,
    ) -> "ExternalValidationSubmissionResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class ArtifactEvidenceReferenceResponseDto(CamelModel):
    artifact_id: str
    artifact_version: str
    artifact_hash: str


class SubmitExternalValidationResponseDto(CamelModel):
    requirement: RequirementResponseDto
    submission: ExternalValidationSubmissionResponseDto
    outbox_topic: str

    @classmethod
    def from_domain(
        cls,
        value: SubmitExternalValidationResult,
    ) -> "SubmitExternalValidationResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            submission=ExternalValidationSubmissionResponseDto.from_domain(value.submission),
            outbox_topic=value.outbox_topic,
        )


class RequirementDeliverySnapshotResponseDto(CamelModel):
    id: UUID
    requirement_id: UUID
    requirement_version: int
    required_work_item_set_version: int
    required_work_item_set_hash: str
    work_item_ids: list[UUID]
    snapshot_hash: str
    created_by: str
    created_at: datetime | None

    @classmethod
    def from_domain(
        cls,
        value: RequirementDeliverySnapshot,
    ) -> "RequirementDeliverySnapshotResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class RequestIntegrationBaselineResponseDto(CamelModel):
    requirement: RequirementResponseDto
    snapshot: RequirementDeliverySnapshotResponseDto
    outbox_topic: str

    @classmethod
    def from_domain(
        cls,
        value: RequestIntegrationBaselineResult,
    ) -> "RequestIntegrationBaselineResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            snapshot=RequirementDeliverySnapshotResponseDto.from_domain(value.snapshot),
            outbox_topic=value.outbox_topic,
        )


class IntegrationBaselineSelectionResponseDto(CamelModel):
    id: UUID
    requirement_id: UUID
    delivery_snapshot_id: UUID
    integration_baseline_id: UUID
    integration_baseline_hash: str
    evidence_requirement_version: int
    evidence_required_work_item_set_version: int
    evidence_required_work_item_set_hash: str
    requirement_version_before: int
    requirement_version_after: int
    selected_by: str
    selected_at: datetime
    invalidated_at: datetime | None
    invalidation_reason: str | None

    @classmethod
    def from_domain(
        cls,
        value: IntegrationBaselineSelectionDto,
    ) -> "IntegrationBaselineSelectionResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class DeliveryGateResponseDto(CamelModel):
    id: UUID
    gate_type: DeliveryGateType
    requirement_id: UUID
    work_item_id: UUID | None
    selection_id: UUID
    requirement_version: int
    acceptance_criteria_version: int
    acceptance_criteria_hash: str
    integration_baseline_id: UUID
    integration_baseline_hash: str
    formal_merge_request_binding_id: UUID | None
    subject_head_sha: str | None
    policy_code: str
    policy_version: int
    policy_snapshot_hash: str
    state: DeliveryGateState
    revision: int
    created_at: datetime
    decided_at: datetime | None
    invalidated_at: datetime | None
    invalidation_reason: str | None

    @classmethod
    def from_domain(cls, value: DeliveryGateDto) -> "DeliveryGateResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class DeliveryGateAssignmentResponseDto(CamelModel):
    id: UUID
    gate_id: UUID
    default_reviewer_id: str
    current_reviewer_id: str
    resolution_snapshot: dict[str, object]
    revision: int
    assigned_at: datetime
    superseded_at: datetime | None

    @classmethod
    def from_domain(
        cls,
        value: DeliveryGateAssignmentDto,
    ) -> "DeliveryGateAssignmentResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class DeliveryDecisionResponseDto(CamelModel):
    id: UUID
    gate_id: UUID
    gate_assignment_id: UUID
    reviewer_id: str
    outcome: DecisionOutcome
    reason: str
    subject_revision: int
    requirement_version: int
    acceptance_criteria_version: int
    acceptance_criteria_hash: str
    integration_baseline_id: UUID
    integration_baseline_hash: str
    subject_head_sha: str | None
    eligibility_snapshot: dict[str, object]
    validity: str
    decided_at: datetime
    invalidated_at: datetime | None
    invalidation_reason: str | None

    @classmethod
    def from_domain(
        cls,
        value: DeliveryDecisionDto,
    ) -> "DeliveryDecisionResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class SelectIntegrationBaselineResponseDto(CamelModel):
    requirement: RequirementResponseDto
    selection: IntegrationBaselineSelectionResponseDto
    outbox_topic: str

    @classmethod
    def from_domain(
        cls,
        value: SelectIntegrationBaselineResult,
    ) -> "SelectIntegrationBaselineResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            selection=IntegrationBaselineSelectionResponseDto.from_domain(value.selection),
            outbox_topic=value.outbox_topic,
        )


class AcceptanceConfirmationResponseDto(CamelModel):
    requirement: RequirementResponseDto
    selection: IntegrationBaselineSelectionResponseDto
    gate: DeliveryGateResponseDto
    assignment: DeliveryGateAssignmentResponseDto

    @classmethod
    def from_domain(
        cls,
        value: AcceptanceConfirmationResult,
    ) -> "AcceptanceConfirmationResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            selection=IntegrationBaselineSelectionResponseDto.from_domain(value.selection),
            gate=DeliveryGateResponseDto.from_domain(value.gate),
            assignment=DeliveryGateAssignmentResponseDto.from_domain(value.assignment),
        )


class AcceptanceDecisionResponseDto(CamelModel):
    requirement: RequirementResponseDto
    selection: IntegrationBaselineSelectionResponseDto
    gate: DeliveryGateResponseDto
    assignment: DeliveryGateAssignmentResponseDto
    decision: DeliveryDecisionResponseDto

    @classmethod
    def from_domain(
        cls,
        value: AcceptanceDecisionResult,
    ) -> "AcceptanceDecisionResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            selection=IntegrationBaselineSelectionResponseDto.from_domain(value.selection),
            gate=DeliveryGateResponseDto.from_domain(value.gate),
            assignment=DeliveryGateAssignmentResponseDto.from_domain(value.assignment),
            decision=DeliveryDecisionResponseDto.from_domain(value.decision),
        )


class FormalDeliveryCommandResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_item: WorkItemResponseDto
    outbox_topic: str

    @classmethod
    def from_domain(
        cls,
        value: FormalDeliveryCommandResult,
    ) -> "FormalDeliveryCommandResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_item=WorkItemResponseDto.from_domain(value.work_item),
            outbox_topic=value.outbox_topic,
        )


class CreateRequirementResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_item: WorkItemResponseDto

    @classmethod
    def from_domain(cls, value: CreateRequirementResult) -> "CreateRequirementResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_item=WorkItemResponseDto.from_domain(value.work_item),
        )


class WorkItemDeliveryResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_item: "WorkItemDeliveryProjectionResponseDto"

    @classmethod
    def from_domain(cls, value: WorkItemDeliveryResult) -> "WorkItemDeliveryResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_item=WorkItemDeliveryProjectionResponseDto.from_domain(value.work_item),
        )


class WorkItemDeliveryProjectionResponseDto(CamelModel):
    id: UUID
    requirement_id: UUID
    human_owner_id: str | None
    assignment_state: AssignmentState
    repository_state: RepositoryState
    state: WorkItemState
    repository_id: str
    integration_delivery_state: IntegrationDeliveryState
    integration_merge_request_binding_id: UUID | None
    integration_blocked_reason_code: IntegrationDeliveryBlockedReason | None
    integration_updated_at: datetime | None
    revision: int

    @classmethod
    def from_domain(cls, value: WorkItemDeliveryDto) -> "WorkItemDeliveryProjectionResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class RequirementListResponseDto(CamelModel):
    items: list[RequirementResponseDto]
    next_cursor: str | None

    @classmethod
    def from_domain(cls, value: RequirementPage) -> "RequirementListResponseDto":
        return cls(
            items=[RequirementResponseDto.from_domain(item) for item in value.items],
            next_cursor=value.next_cursor,
        )


class SddBaselineResponseDto(CamelModel):
    id: UUID
    requirement_id: UUID
    requirement_version: int
    artifact_id: str
    artifact_version: str
    artifact_hash: str
    route_snapshot_version: int
    route_snapshot_hash: str
    created_by: str
    created_at: datetime

    @classmethod
    def from_domain(cls, value: SddBaselineDto) -> "SddBaselineResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class GateInstanceResponseDto(CamelModel):
    id: UUID
    gate_type: GateType
    requirement_id: UUID
    requirement_version: int
    sdd_baseline_id: UUID
    artifact_id: str
    artifact_version: str
    artifact_hash: str
    route_snapshot_version: int
    route_snapshot_hash: str
    policy_code: str
    policy_version: int
    policy_snapshot_hash: str
    state: GateState
    revision: int
    created_at: datetime
    decided_at: datetime | None

    @classmethod
    def from_domain(cls, value: GateInstanceDto) -> "GateInstanceResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class GateAssignmentResponseDto(CamelModel):
    id: UUID
    gate_instance_id: UUID
    default_reviewer_id: str
    current_reviewer_id: str
    revision: int
    assigned_at: datetime
    superseded_at: datetime | None

    @classmethod
    def from_domain(cls, value: GateAssignmentDto) -> "GateAssignmentResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class DecisionResponseDto(CamelModel):
    id: UUID
    gate_instance_id: UUID
    gate_assignment_id: UUID
    reviewer_id: str
    outcome: DecisionOutcome
    reason: str
    subject_revision: int
    decided_at: datetime

    @classmethod
    def from_domain(cls, value: DecisionDto) -> "DecisionResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class RegisterSddBaselineResponseDto(CamelModel):
    requirement: RequirementResponseDto
    baseline: SddBaselineResponseDto

    @classmethod
    def from_domain(cls, value: RegisterSddBaselineResult) -> "RegisterSddBaselineResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            baseline=SddBaselineResponseDto.from_domain(value.baseline),
        )


class BaselineConfirmationResponseDto(CamelModel):
    requirement: RequirementResponseDto
    gate: GateInstanceResponseDto
    assignment: GateAssignmentResponseDto

    @classmethod
    def from_domain(
        cls,
        value: BaselineConfirmationResult,
    ) -> "BaselineConfirmationResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            gate=GateInstanceResponseDto.from_domain(value.gate),
            assignment=GateAssignmentResponseDto.from_domain(value.assignment),
        )


class BaselineDecisionResponseDto(CamelModel):
    requirement: RequirementResponseDto
    gate: GateInstanceResponseDto
    decision: DecisionResponseDto

    @classmethod
    def from_domain(cls, value: BaselineDecisionResult) -> "BaselineDecisionResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            gate=GateInstanceResponseDto.from_domain(value.gate),
            decision=DecisionResponseDto.from_domain(value.decision),
        )


class WorkItemAssignmentResponseDto(CamelModel):
    id: UUID
    work_item_id: UUID
    assignee_id: str
    assigned_by: str
    reason: str
    revision: int
    assigned_at: datetime
    superseded_at: datetime | None

    @classmethod
    def from_domain(cls, value: WorkItemAssignmentDto) -> "WorkItemAssignmentResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class SddArtifactVersionResponseDto(CamelModel):
    artifact_id: UUID
    version: int
    requirement_id: UUID
    sha256: str
    state: str
    media_type: str
    trust: str
    content: str
    created_by: str
    created_at: datetime

    @classmethod
    def from_domain(cls, value: SddArtifactVersionDto) -> "SddArtifactVersionResponseDto":
        return cls.model_validate(value.model_dump(mode="json"))


class CreateSddArtifactResponseDto(CamelModel):
    requirement: RequirementResponseDto
    artifact: SddArtifactVersionResponseDto

    @classmethod
    def from_domain(cls, value: CreateSddArtifactResult) -> "CreateSddArtifactResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            artifact=SddArtifactVersionResponseDto.from_domain(value.artifact),
        )


class AddWorkItemResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_item: WorkItemResponseDto
    assignment: WorkItemAssignmentResponseDto | None

    @classmethod
    def from_domain(cls, value: AddWorkItemResult) -> "AddWorkItemResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_item=WorkItemResponseDto.from_domain(value.work_item),
            assignment=(
                None
                if value.assignment is None
                else WorkItemAssignmentResponseDto.from_domain(value.assignment)
            ),
        )


class AssignWorkItemResponseDto(CamelModel):
    work_item: WorkItemResponseDto
    assignment: WorkItemAssignmentResponseDto

    @classmethod
    def from_domain(cls, value: AssignWorkItemResult) -> "AssignWorkItemResponseDto":
        return cls(
            work_item=WorkItemResponseDto.from_domain(value.work_item),
            assignment=WorkItemAssignmentResponseDto.from_domain(value.assignment),
        )


class GateReassignmentResponseDto(CamelModel):
    gate: GateInstanceResponseDto
    assignment: GateAssignmentResponseDto

    @classmethod
    def from_domain(cls, value: GateReassignmentResult) -> "GateReassignmentResponseDto":
        return cls(
            gate=GateInstanceResponseDto.from_domain(value.gate),
            assignment=GateAssignmentResponseDto.from_domain(value.assignment),
        )


class RequirementDetailsResponseDto(CamelModel):
    requirement: RequirementResponseDto
    work_items: list[WorkItemResponseDto]
    work_item_assignments: list[WorkItemAssignmentResponseDto]
    current_sdd_baseline: SddBaselineResponseDto | None
    current_gate: GateInstanceResponseDto | None
    current_gate_assignment: GateAssignmentResponseDto | None
    current_decision: DecisionResponseDto | None

    @classmethod
    def from_domain(cls, value: RequirementDetailsDto) -> "RequirementDetailsResponseDto":
        return cls(
            requirement=RequirementResponseDto.from_domain(value.requirement),
            work_items=[WorkItemResponseDto.from_domain(item) for item in value.work_items],
            work_item_assignments=[
                WorkItemAssignmentResponseDto.from_domain(item)
                for item in value.work_item_assignments
            ],
            current_sdd_baseline=(
                None
                if value.current_sdd_baseline is None
                else SddBaselineResponseDto.from_domain(value.current_sdd_baseline)
            ),
            current_gate=(
                None
                if value.current_gate is None
                else GateInstanceResponseDto.from_domain(value.current_gate)
            ),
            current_gate_assignment=(
                None
                if value.current_gate_assignment is None
                else GateAssignmentResponseDto.from_domain(value.current_gate_assignment)
            ),
            current_decision=(
                None
                if value.current_decision is None
                else DecisionResponseDto.from_domain(value.current_decision)
            ),
        )
