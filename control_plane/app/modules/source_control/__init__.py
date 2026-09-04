"""Public Source Control facade; other modules must not import internals."""

from sqlalchemy import Connection

from control_plane.app.modules.source_control.application import (
    ProcessIntegrationRequestResult,
    SourceControlDependencies,
    accept_binding_request,
    authorize_agent_push,
    binding_request_payload_hash,
    execute_agent_push,
    fence_agent_attempt,
    get_agent_delivery,
    get_repository_branch_binding,
    ingest_signed_gitlab_webhook,
    process_binding_request,
    process_due_source_control_inboxes,
    process_formal_delivery_request,
    process_integration_merge_request,
    process_integration_mr_request,
    process_webhook_inbox,
    reconcile_agent_pushes,
    reconcile_agent_revocations,
    reconcile_due_effects,
    reconcile_due_integration_effects,
    reconcile_due_source_control_effects,
    reconcile_formal_delivery_effect,
    register_workspace_repository,
    relay_binding_requests,
    relay_due_source_control_requests,
    relay_requirement_evidence_requests,
    relay_requirement_formal_delivery_requests,
    remove_workspace_repository,
    verify_gitlab_standard_webhook,
)
from control_plane.app.modules.source_control.application import (
    accept_external_validation as _accept_external_validation,
)
from control_plane.app.modules.source_control.application import (
    accept_formal_delivery_request as _accept_formal_delivery_request,
)
from control_plane.app.modules.source_control.application import (
    accept_integration_baseline_request as _accept_integration_baseline_request,
)
from control_plane.app.modules.source_control.application import (
    get_integration_baseline_evidence as _get_integration_baseline_evidence,
)
from control_plane.app.modules.source_control.application import (
    list_authorized_repositories as _list_authorized_repositories,
)
from control_plane.app.modules.source_control.application import (
    process_integration_baseline_request as _process_integration_baseline_request,
)
from control_plane.app.modules.source_control.application import (
    validate_authorized_repository_runtime as _validate_authorized_repository_runtime,
)
from control_plane.app.modules.source_control.application.evidence import (
    get_integration_baseline_evidence_by_snapshot as _get_integration_baseline_evidence_by_snapshot,
)
from control_plane.app.modules.source_control.domain import (
    AgentDeliveryBatchResult,
    AgentDeliveryDto,
    AgentExecutionBindingSnapshot,
    AgentFenceResult,
    AgentPushBindingRejected,
    AgentPushGrantResult,
    AgentPushIdempotencyConflict,
    AgentPushNotFound,
    AgentPushRequestSpec,
    AgentPushState,
    AgentRevocationBatchResult,
    ArtifactReference,
    AuthorizedRepositorySummaryDto,
    BindingRequestEnvelope,
    BindingRequestInboxDto,
    BindingRequestMessageConflict,
    EffectOperation,
    EffectState,
    EvidenceMessageConflict,
    EvidenceStale,
    EvidenceUnavailable,
    ExternalValidationReference,
    ExternalValidationRequestEnvelope,
    FormalDeliveryConflict,
    FormalDeliveryRequestEnvelope,
    FormalDeliveryRequestKind,
    FormalReviewAssignmentDto,
    FormalReviewRoutingSnapshot,
    GitLabWebhookEnvelope,
    InboxState,
    IntegrationBaselineEvidence,
    IntegrationBaselineEvidenceItem,
    IntegrationBaselineRequestEnvelope,
    InvalidBranchName,
    InvalidEffectTransition,
    InvalidRepositorySecretReference,
    MergeRequestBindingDto,
    MergeRequestCreationOrigin,
    MergeRequestKind,
    MergeRequestObservationDto,
    MergeRequestState,
    ProcessBindingRequestResult,
    ProcessFormalDeliveryResult,
    ReconcileDueEffectsResult,
    ReconcileDueIntegrationEffectsResult,
    RelayBindingRequestsResult,
    RelayFormalDeliveryRequestsResult,
    RepositoryAuthorizationState,
    RepositoryBranchBindingDto,
    RepositoryNotFound,
    RepositoryRemoved,
    RepositoryWorkspaceConflict,
    RequirementCallbackState,
    RequirementCallbackUnavailable,
    SourceControlBatchResult,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
    SourceControlError,
    StaleRepositoryRevision,
    VerifiedStandardWebhook,
    WebhookIdConflict,
    WebhookInboxDto,
    WebhookInboxState,
    WebhookPayloadInvalid,
    WebhookReplayRejected,
    WebhookSignatureInvalid,
    WorkspaceRepositoryDto,
    build_task_branch_name,
    transition_effect,
)
from control_plane.app.modules.source_control.ports import (
    AgentDeliveryDependencyUnavailable,
    RelayEvidenceRequestsResult,
    SecretReferencePort,
    SourceControlEvidenceRepository,
)


def _evidence_repository(
    db: Connection,
    dependencies: SourceControlDependencies,
) -> SourceControlEvidenceRepository:
    factory = dependencies.evidence_repository_factory
    if factory is None:
        raise SourceControlDependencyUnavailable("Source Control Evidence repository unavailable")
    return factory(db)


def accept_external_validation(
    db: Connection,
    envelope: ExternalValidationRequestEnvelope,
    *,
    dependencies: SourceControlDependencies,
) -> ExternalValidationReference:
    return _accept_external_validation(
        _evidence_repository(db, dependencies),
        envelope,
        dependencies=dependencies,
    )


def accept_integration_baseline_request(
    db: Connection,
    envelope: IntegrationBaselineRequestEnvelope,
    *,
    dependencies: SourceControlDependencies,
) -> bool:
    return _accept_integration_baseline_request(
        _evidence_repository(db, dependencies),
        envelope,
        dependencies=dependencies,
    )


def accept_formal_delivery_request(
    db: Connection,
    envelope: FormalDeliveryRequestEnvelope,
    *,
    dependencies: SourceControlDependencies,
) -> bool:
    factory = dependencies.formal_repository_factory
    if factory is None:
        raise SourceControlDependencyUnavailable("Formal Delivery repository unavailable")
    return _accept_formal_delivery_request(
        factory(db),
        envelope,
        dependencies=dependencies,
    )


def process_integration_baseline_request(
    db: Connection,
    *,
    message_id: str,
    generated_by: str,
    dependencies: SourceControlDependencies,
) -> IntegrationBaselineEvidence:
    return _process_integration_baseline_request(
        _evidence_repository(db, dependencies),
        message_id=message_id,
        generated_by=generated_by,
        dependencies=dependencies,
    )


def get_integration_baseline_evidence(
    db: Connection,
    *,
    evidence_id: str,
    dependencies: SourceControlDependencies,
) -> IntegrationBaselineEvidence:
    return _get_integration_baseline_evidence(
        _evidence_repository(db, dependencies),
        evidence_id=evidence_id,
    )


def get_integration_baseline_evidence_by_snapshot(
    db: Connection,
    *,
    delivery_snapshot_id: str,
    delivery_snapshot_hash: str,
    dependencies: SourceControlDependencies,
) -> IntegrationBaselineEvidence:
    return _get_integration_baseline_evidence_by_snapshot(
        _evidence_repository(db, dependencies),
        delivery_snapshot_id=delivery_snapshot_id,
        delivery_snapshot_hash=delivery_snapshot_hash,
    )


def list_authorized_repositories(
    db: Connection,
    *,
    workspace_id: str,
    dependencies: SourceControlDependencies,
) -> tuple[AuthorizedRepositorySummaryDto, ...]:
    return _list_authorized_repositories(
        dependencies.repository_factory(db),
        workspace_id=workspace_id,
    )


def validate_authorized_repository_runtime(
    db: Connection,
    *,
    dependencies: SourceControlDependencies,
    secrets: SecretReferencePort,
    connection_ref: str,
) -> None:
    _validate_authorized_repository_runtime(
        dependencies.repository_factory(db),
        secrets=secrets,
        connection_ref=connection_ref,
    )


__all__ = [
    "AgentDeliveryDependencyUnavailable",
    "AgentDeliveryBatchResult",
    "AgentDeliveryDto",
    "AgentExecutionBindingSnapshot",
    "AgentFenceResult",
    "AgentPushBindingRejected",
    "AgentPushGrantResult",
    "AgentPushIdempotencyConflict",
    "AgentPushNotFound",
    "AgentPushRequestSpec",
    "AgentPushState",
    "AgentRevocationBatchResult",
    "ArtifactReference",
    "AuthorizedRepositorySummaryDto",
    "BindingRequestEnvelope",
    "BindingRequestInboxDto",
    "BindingRequestMessageConflict",
    "EffectState",
    "EffectOperation",
    "EvidenceMessageConflict",
    "EvidenceStale",
    "EvidenceUnavailable",
    "ExternalValidationReference",
    "ExternalValidationRequestEnvelope",
    "FormalDeliveryConflict",
    "FormalDeliveryRequestEnvelope",
    "FormalDeliveryRequestKind",
    "FormalReviewAssignmentDto",
    "FormalReviewRoutingSnapshot",
    "GitLabWebhookEnvelope",
    "InboxState",
    "IntegrationBaselineEvidence",
    "IntegrationBaselineEvidenceItem",
    "IntegrationBaselineRequestEnvelope",
    "InvalidBranchName",
    "InvalidEffectTransition",
    "InvalidRepositorySecretReference",
    "MergeRequestBindingDto",
    "MergeRequestCreationOrigin",
    "MergeRequestKind",
    "MergeRequestObservationDto",
    "MergeRequestState",
    "ProcessIntegrationRequestResult",
    "ProcessBindingRequestResult",
    "ProcessFormalDeliveryResult",
    "ReconcileDueEffectsResult",
    "ReconcileDueIntegrationEffectsResult",
    "RepositoryAuthorizationState",
    "RepositoryBranchBindingDto",
    "RepositoryNotFound",
    "RepositoryRemoved",
    "RepositoryWorkspaceConflict",
    "RequirementCallbackState",
    "RequirementCallbackUnavailable",
    "RelayBindingRequestsResult",
    "RelayEvidenceRequestsResult",
    "RelayFormalDeliveryRequestsResult",
    "SourceControlDependencies",
    "SourceControlBatchResult",
    "SourceControlDependencyUnavailable",
    "SourceControlError",
    "StaleRepositoryRevision",
    "VerifiedStandardWebhook",
    "WebhookIdConflict",
    "WebhookInboxDto",
    "SourceControlEffectDto",
    "WebhookInboxState",
    "WebhookPayloadInvalid",
    "WebhookReplayRejected",
    "WebhookSignatureInvalid",
    "WorkspaceRepositoryDto",
    "accept_binding_request",
    "authorize_agent_push",
    "binding_request_payload_hash",
    "build_task_branch_name",
    "get_repository_branch_binding",
    "execute_agent_push",
    "fence_agent_attempt",
    "get_agent_delivery",
    "accept_external_validation",
    "accept_formal_delivery_request",
    "accept_integration_baseline_request",
    "get_integration_baseline_evidence",
    "get_integration_baseline_evidence_by_snapshot",
    "ingest_signed_gitlab_webhook",
    "list_authorized_repositories",
    "process_binding_request",
    "process_due_source_control_inboxes",
    "process_formal_delivery_request",
    "process_integration_merge_request",
    "process_integration_mr_request",
    "process_integration_baseline_request",
    "reconcile_due_integration_effects",
    "reconcile_formal_delivery_effect",
    "process_webhook_inbox",
    "reconcile_due_effects",
    "reconcile_agent_pushes",
    "reconcile_agent_revocations",
    "reconcile_due_source_control_effects",
    "register_workspace_repository",
    "relay_binding_requests",
    "relay_due_source_control_requests",
    "relay_requirement_evidence_requests",
    "relay_requirement_formal_delivery_requests",
    "remove_workspace_repository",
    "transition_effect",
    "validate_authorized_repository_runtime",
    "verify_gitlab_standard_webhook",
]
