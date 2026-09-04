from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExecutionKind(StrEnum):
    SINGLE_REPOSITORY_FIX = "SINGLE_REPOSITORY_FIX"


class RepositoryAccessMode(StrEnum):
    READ_WRITE_WORKTREE = "READ_WRITE_WORKTREE"


class MaterializationState(StrEnum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    RELEASING = "RELEASING"
    RELEASED = "RELEASED"
    FINALIZING = "FINALIZING"
    FINALIZED = "FINALIZED"
    CANCELING = "CANCELING"
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


class EvidenceKind(StrEnum):
    CHECKPOINT = "CHECKPOINT"
    PATCH = "PATCH"
    LOG = "LOG"
    TEST_RESULT = "TEST_RESULT"
    PREVIEW_METADATA = "PREVIEW_METADATA"
    DIAGNOSTIC = "DIAGNOSTIC"


class DenialCode(StrEnum):
    CAPACITY_UNAVAILABLE = "CAPACITY_UNAVAILABLE"
    POLICY_LIMIT_REACHED = "POLICY_LIMIT_REACHED"
    POLICY_DISABLED = "POLICY_DISABLED"
    RUNTIME_BINDING_INVALID = "RUNTIME_BINDING_INVALID"
    RUNTIME_CAPABILITY_DENIED = "RUNTIME_CAPABILITY_DENIED"
    RUNTIME_BOUNDARY_VIOLATION = "RUNTIME_BOUNDARY_VIOLATION"
    STALE_RUNNER_GENERATION = "STALE_RUNNER_GENERATION"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"


class CancellationReason(StrEnum):
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"
    TERMINATED = "TERMINATED"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"


class VersionedProfileRef(FrozenModel):
    schema_version: Literal[1] = 1
    id: NonEmptyStr
    version: NonEmptyStr
    digest: Digest


class ResourceProfileRef(VersionedProfileRef):
    unit_weight: PositiveInt


class ExecutionRef(FrozenModel):
    schema_version: Literal[1] = 1
    execution_id: NonEmptyStr
    kind: ExecutionKind


class SandboxEnvironmentRef(FrozenModel):
    schema_version: Literal[1] = 1
    environment_id: NonEmptyStr
    workspace_id: NonEmptyStr
    requirement_id: NonEmptyStr


class RepositoryCheckout(FrozenModel):
    schema_version: Literal[1] = 1
    repository_id: NonEmptyStr
    branch_name: NonEmptyStr
    base_commit_sha: CommitSha
    access_mode: RepositoryAccessMode = RepositoryAccessMode.READ_WRITE_WORKTREE

    @model_validator(mode="after")
    def validate_branch(self) -> "RepositoryCheckout":
        branch = self.branch_name
        if (
            branch.startswith(("/", "."))
            or branch.endswith(("/", "."))
            or ".." in branch
            or "//" in branch
            or any(character.isspace() for character in branch)
        ):
            raise ValueError("branch name is invalid")
        return self


class RunnerManifestRef(FrozenModel):
    schema_version: Literal[1] = 1
    image_digest: Digest
    protocol_version: NonEmptyStr
    runtime_engine_id: NonEmptyStr
    adapter_version: NonEmptyStr
    bundle_digest: Digest


class BoundaryManifest(FrozenModel):
    schema_version: Literal[1] = 1
    allowed_tool_ids: tuple[NonEmptyStr, ...]
    allowed_network_target_refs: tuple[NonEmptyStr, ...]
    secret_lease_ref: NonEmptyStr
    repository_checkout: RepositoryCheckout
    tool_policy: VersionedProfileRef
    network_policy: VersionedProfileRef
    secret_scope: VersionedProfileRef
    preview_enabled: bool = False

    @model_validator(mode="after")
    def validate_unique_authorities(self) -> "BoundaryManifest":
        if not self.allowed_tool_ids:
            raise ValueError("at least one tool capability is required")
        if len(set(self.allowed_tool_ids)) != len(self.allowed_tool_ids):
            raise ValueError("tool capabilities must be unique")
        if len(set(self.allowed_network_target_refs)) != len(self.allowed_network_target_refs):
            raise ValueError("network targets must be unique")
        return self


class ExecutionBindingProjection(FrozenModel):
    schema_version: Literal[1] = 1
    execution: ExecutionRef
    environment: SandboxEnvironmentRef
    binding_digest: Digest
    deadline_at: AwareDatetime
    runtime_profile: VersionedProfileRef
    resource_profile: ResourceProfileRef
    runner_manifest: RunnerManifestRef
    boundaries: BoundaryManifest
    policy_versions: tuple[VersionedProfileRef, ...]

    @model_validator(mode="after")
    def validate_policy_versions(self) -> "ExecutionBindingProjection":
        if not self.policy_versions:
            raise ValueError("at least one policy version is required")
        policy_ids = [policy.id for policy in self.policy_versions]
        if len(set(policy_ids)) != len(policy_ids):
            raise ValueError("policy identifiers must be unique")
        return self


class CommandContext(FrozenModel):
    schema_version: Literal[1] = 1
    idempotency_key: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ]
    actor: NonEmptyStr
    correlation_id: NonEmptyStr
    causation_id: NonEmptyStr | None = None
    request_id: NonEmptyStr | None = None


class MaterializationHandle(FrozenModel):
    schema_version: Literal[1] = 1
    materialization_id: NonEmptyStr
    environment_id: NonEmptyStr
    execution_id: NonEmptyStr
    lease_id: NonEmptyStr
    generation: PositiveInt
    fencing_token: SecretStr
    revision: PositiveInt
    deadline_at: AwareDatetime


class MaterializationGuard(FrozenModel):
    schema_version: Literal[1] = 1
    materialization_id: NonEmptyStr
    lease_id: NonEmptyStr
    generation: PositiveInt
    fencing_token: SecretStr
    expected_revision: PositiveInt


class EvidenceRef(FrozenModel):
    schema_version: Literal[1] = 1
    kind: EvidenceKind
    artifact_id: NonEmptyStr
    version: NonEmptyStr
    sha256: Digest
    classification: NonEmptyStr


class CanonicalDenial(FrozenModel):
    schema_version: Literal[1] = 1
    code: DenialCode
    failure_dimension: NonEmptyStr | None = None
    policy_version: NonEmptyStr | None = None
    diagnostic_ref: EvidenceRef | None = None
    retryable: bool = False


class ProvisionMaterializationCommand(FrozenModel):
    schema_version: Literal[1] = 1
    context: CommandContext
    binding: ExecutionBindingProjection


class MaterializationReady(FrozenModel):
    kind: Literal["READY"] = "READY"
    handle: MaterializationHandle
    binding_digest: Digest
    runner_protocol_version: NonEmptyStr
    lab_only: bool


class MaterializationBlocked(FrozenModel):
    kind: Literal["BLOCKED"] = "BLOCKED"
    execution: ExecutionRef
    binding_digest: Digest
    denial: CanonicalDenial


class MaterializationFailed(FrozenModel):
    kind: Literal["FAILED"] = "FAILED"
    execution: ExecutionRef
    binding_digest: Digest
    denial: CanonicalDenial


ProvisionResult = MaterializationReady | MaterializationBlocked | MaterializationFailed


class GetMaterializationStatusQuery(FrozenModel):
    schema_version: Literal[1] = 1
    actor: NonEmptyStr
    materialization_id: NonEmptyStr


class MaterializationStatus(FrozenModel):
    schema_version: Literal[1] = 1
    materialization_id: NonEmptyStr
    environment_id: NonEmptyStr
    execution_id: NonEmptyStr
    state: MaterializationState
    revision: PositiveInt
    generation: PositiveInt
    binding_digest: Digest
    deadline_at: AwareDatetime
    evidence_refs: tuple[EvidenceRef, ...] = ()
    denial: CanonicalDenial | None = None


class PublishPreviewCommand(FrozenModel):
    schema_version: Literal[1] = 1
    context: CommandContext
    guard: MaterializationGuard
    metadata: EvidenceRef
    expires_at: AwareDatetime


class PreviewPublished(FrozenModel):
    kind: Literal["PUBLISHED"] = "PUBLISHED"
    preview_id: NonEmptyStr
    access_ref: NonEmptyStr
    expires_at: AwareDatetime
    revision: PositiveInt


class SandboxDenied(FrozenModel):
    kind: Literal["DENIED"] = "DENIED"
    denial: CanonicalDenial
    revision: PositiveInt | None = None


PreviewResult = PreviewPublished | SandboxDenied


class CheckpointAndReleaseCommand(FrozenModel):
    schema_version: Literal[1] = 1
    context: CommandContext
    guard: MaterializationGuard
    evidence_refs: tuple[EvidenceRef, ...]


class HandoffToChildCommand(FrozenModel):
    schema_version: Literal[1] = 1
    context: CommandContext
    guard: MaterializationGuard
    child_execution_id: NonEmptyStr


class FinalizeExecutionCommand(FrozenModel):
    schema_version: Literal[1] = 1
    context: CommandContext
    guard: MaterializationGuard
    evidence_refs: tuple[EvidenceRef, ...]


class CancelExecutionCommand(FrozenModel):
    schema_version: Literal[1] = 1
    context: CommandContext
    execution: ExecutionRef
    reason: CancellationReason


class ReconcileLeaseCommand(FrozenModel):
    schema_version: Literal[1] = 1
    context: CommandContext
    environment_id: NonEmptyStr
    execution_id: NonEmptyStr | None = None
    observed_at: AwareDatetime


class LifecycleReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    operation: NonEmptyStr
    materialization_id: NonEmptyStr
    state: MaterializationState
    revision: PositiveInt
    evidence_refs: tuple[EvidenceRef, ...] = ()
    denial: CanonicalDenial | None = None


ReleaseReceipt = LifecycleReceipt
HandoffResult = LifecycleReceipt
FinalizationReceipt = LifecycleReceipt
CancellationReceipt = LifecycleReceipt


class LeaseReconciliationItem(FrozenModel):
    schema_version: Literal[1] = 1
    materialization_id: NonEmptyStr
    state: MaterializationState
    revision: PositiveInt
    action: NonEmptyStr


class ReconciliationReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    environment_id: NonEmptyStr
    observed_at: AwareDatetime
    items: tuple[LeaseReconciliationItem, ...]
    reconciled_count: NonNegativeInt


def aware_now(value: datetime) -> datetime:
    """Narrow helper used by adapters that must preserve timezone awareness."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value
