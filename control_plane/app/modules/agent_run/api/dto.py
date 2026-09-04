from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, SecretStr
from pydantic.alias_generators import to_camel

from control_plane.app.modules.agent_run.domain import (
    BoundaryManifest,
    CancellationReason,
    CanonicalDenial,
    EvidenceKind,
    EvidenceRef,
    ExecutionBindingProjection,
    ExecutionKind,
    ExecutionRef,
    LeaseReconciliationItem,
    LifecycleReceipt,
    MaterializationGuard,
    MaterializationReady,
    MaterializationState,
    MaterializationStatus,
    PreviewPublished,
    ReconciliationReceipt,
    RepositoryAccessMode,
    RepositoryCheckout,
    ResourceProfileRef,
    RunnerManifestRef,
    SandboxEnvironmentRef,
    VersionedProfileRef,
)
from control_plane.app.modules.agent_run.domain.models import (
    CommitSha,
    Digest,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
)
from control_plane.app.shared.api.camel import CamelModel


class SandboxApiModel(CamelModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class VersionedProfileRefDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    id: NonEmptyStr
    version: NonEmptyStr
    digest: Digest

    def to_domain(self) -> VersionedProfileRef:
        return VersionedProfileRef.model_validate(self.model_dump())


class ResourceProfileRefDto(VersionedProfileRefDto):
    unit_weight: PositiveInt

    def to_domain(self) -> ResourceProfileRef:
        return ResourceProfileRef.model_validate(self.model_dump())


class ExecutionRefDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    execution_id: NonEmptyStr
    kind: ExecutionKind

    def to_domain(self) -> ExecutionRef:
        return ExecutionRef.model_validate(self.model_dump())


class SandboxEnvironmentRefDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    environment_id: NonEmptyStr
    workspace_id: NonEmptyStr
    requirement_id: NonEmptyStr

    def to_domain(self) -> SandboxEnvironmentRef:
        return SandboxEnvironmentRef.model_validate(self.model_dump())


class RepositoryCheckoutDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    repository_id: NonEmptyStr
    branch_name: NonEmptyStr
    base_commit_sha: CommitSha
    access_mode: RepositoryAccessMode = RepositoryAccessMode.READ_WRITE_WORKTREE

    def to_domain(self) -> RepositoryCheckout:
        return RepositoryCheckout.model_validate(self.model_dump())


class RunnerManifestRefDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    image_digest: Digest
    protocol_version: NonEmptyStr
    runtime_engine_id: NonEmptyStr
    adapter_version: NonEmptyStr
    bundle_digest: Digest

    def to_domain(self) -> RunnerManifestRef:
        return RunnerManifestRef.model_validate(self.model_dump())


class BoundaryManifestDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    allowed_tool_ids: tuple[NonEmptyStr, ...]
    allowed_network_target_refs: tuple[NonEmptyStr, ...]
    secret_lease_ref: NonEmptyStr
    repository_checkout: RepositoryCheckoutDto
    tool_policy: VersionedProfileRefDto
    network_policy: VersionedProfileRefDto
    secret_scope: VersionedProfileRefDto
    preview_enabled: bool = False

    def to_domain(self) -> BoundaryManifest:
        return BoundaryManifest.model_validate(self.model_dump())


class ProvisionMaterializationRequestDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    execution: ExecutionRefDto
    environment: SandboxEnvironmentRefDto
    binding_digest: Digest
    deadline_at: AwareDatetime
    runtime_profile: VersionedProfileRefDto
    resource_profile: ResourceProfileRefDto
    runner_manifest: RunnerManifestRefDto
    boundaries: BoundaryManifestDto
    policy_versions: tuple[VersionedProfileRefDto, ...]

    @classmethod
    def from_domain(
        cls,
        value: ExecutionBindingProjection,
    ) -> "ProvisionMaterializationRequestDto":
        return cls.model_validate(value.model_dump())

    def to_domain(self) -> ExecutionBindingProjection:
        return ExecutionBindingProjection.model_validate(self.model_dump())


class EvidenceRefDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    kind: EvidenceKind
    artifact_id: NonEmptyStr
    version: NonEmptyStr
    sha256: Digest
    classification: NonEmptyStr

    @classmethod
    def from_domain(cls, value: EvidenceRef) -> "EvidenceRefDto":
        return cls.model_validate(value.model_dump())

    def to_domain(self) -> EvidenceRef:
        return EvidenceRef.model_validate(self.model_dump())


class MaterializationMutationDto(SandboxApiModel):
    lease_id: NonEmptyStr
    generation: PositiveInt
    fencing_token: NonEmptyStr

    def to_guard(self, materialization_id: str, expected_revision: int) -> MaterializationGuard:
        return MaterializationGuard(
            materialization_id=materialization_id,
            lease_id=self.lease_id,
            generation=self.generation,
            fencing_token=SecretStr(self.fencing_token),
            expected_revision=expected_revision,
        )


class PublishPreviewRequestDto(MaterializationMutationDto):
    metadata: EvidenceRefDto
    expires_at: AwareDatetime


class EvidenceCleanupRequestDto(MaterializationMutationDto):
    evidence_refs: tuple[EvidenceRefDto, ...]

    def domain_evidence_refs(self) -> tuple[EvidenceRef, ...]:
        return tuple(item.to_domain() for item in self.evidence_refs)


class HandoffToChildRequestDto(MaterializationMutationDto):
    child_execution_id: NonEmptyStr


class CancelExecutionRequestDto(SandboxApiModel):
    reason: CancellationReason


class ReconcileLeaseRequestDto(SandboxApiModel):
    environment_id: NonEmptyStr
    execution_id: NonEmptyStr | None = None
    observed_at: AwareDatetime


class CanonicalDenialDto(SandboxApiModel):
    code: NonEmptyStr
    failure_dimension: NonEmptyStr | None = None
    policy_version: NonEmptyStr | None = None
    diagnostic_ref: EvidenceRefDto | None = None
    retryable: bool = False

    @classmethod
    def from_domain(cls, value: CanonicalDenial) -> "CanonicalDenialDto":
        return cls.model_validate(value.model_dump(mode="json"))


class MaterializationHandleDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    materialization_id: NonEmptyStr
    environment_id: NonEmptyStr
    execution_id: NonEmptyStr
    lease_id: NonEmptyStr
    generation: PositiveInt
    fencing_token: NonEmptyStr
    revision: PositiveInt
    deadline_at: datetime


class MaterializationReadyDto(SandboxApiModel):
    kind: Literal["READY"] = "READY"
    handle: MaterializationHandleDto
    binding_digest: Digest
    runner_protocol_version: NonEmptyStr
    lab_only: bool

    @classmethod
    def from_domain(cls, value: MaterializationReady) -> "MaterializationReadyDto":
        body = value.model_dump(mode="json")
        body["handle"]["fencing_token"] = value.handle.fencing_token.get_secret_value()
        return cls.model_validate(body)


class MaterializationStatusDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    materialization_id: NonEmptyStr
    environment_id: NonEmptyStr
    execution_id: NonEmptyStr
    state: MaterializationState
    revision: PositiveInt
    generation: PositiveInt
    binding_digest: Digest
    deadline_at: datetime
    evidence_refs: tuple[EvidenceRefDto, ...] = ()
    denial: CanonicalDenialDto | None = None

    @classmethod
    def from_domain(cls, value: MaterializationStatus) -> "MaterializationStatusDto":
        return cls.model_validate(value.model_dump(mode="json"))


class PreviewPublishedDto(SandboxApiModel):
    kind: Literal["PUBLISHED"] = "PUBLISHED"
    preview_id: NonEmptyStr
    access_ref: NonEmptyStr
    expires_at: datetime
    revision: PositiveInt

    @classmethod
    def from_domain(cls, value: PreviewPublished) -> "PreviewPublishedDto":
        return cls.model_validate(value.model_dump(mode="json"))


class LifecycleReceiptDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    operation: NonEmptyStr
    materialization_id: NonEmptyStr
    state: MaterializationState
    revision: PositiveInt
    evidence_refs: tuple[EvidenceRefDto, ...] = ()
    denial: CanonicalDenialDto | None = None

    @classmethod
    def from_domain(cls, value: LifecycleReceipt) -> "LifecycleReceiptDto":
        return cls.model_validate(value.model_dump(mode="json"))


class LeaseReconciliationItemDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    materialization_id: NonEmptyStr
    state: MaterializationState
    revision: PositiveInt
    action: NonEmptyStr

    @classmethod
    def from_domain(cls, value: LeaseReconciliationItem) -> "LeaseReconciliationItemDto":
        return cls.model_validate(value.model_dump(mode="json"))


class ReconciliationReceiptDto(SandboxApiModel):
    schema_version: Literal[1] = 1
    environment_id: NonEmptyStr
    observed_at: datetime
    items: tuple[LeaseReconciliationItemDto, ...]
    reconciled_count: NonNegativeInt

    @classmethod
    def from_domain(cls, value: ReconciliationReceipt) -> "ReconciliationReceiptDto":
        return cls.model_validate(value.model_dump(mode="json"))
