from dataclasses import asdict
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.requirement.domain import (
    ArtifactEvidenceReference,
    RequirementType,
)
from control_plane.app.modules.requirement.domain.gate_policy import GatePolicy, content_hash


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class RandomPort(Protocol):
    def uuid4(self) -> object: ...


class RouteSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    snapshot_hash: str
    required_capabilities: tuple[str, ...]
    requirement_type: RequirementType | None = None
    steps: tuple[str, ...] = ()


class RouteSnapshotPort(Protocol):
    def current(self, requirement_type: RequirementType) -> RouteSnapshot: ...


class AssignmentGuardPort(Protocol):
    def can_assign(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: str,
        required_capabilities: tuple[str, ...],
    ) -> bool: ...

    def can_auto_assign(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: str,
        required_capabilities: tuple[str, ...],
    ) -> bool: ...


class ArtifactState(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class ArtifactTrust(StrEnum):
    TRUSTED_PLAIN_TEXT = "TRUSTED_PLAIN_TEXT"
    UNTRUSTED = "UNTRUSTED"


class ArtifactSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    version: str
    sha256: str
    state: ArtifactState
    media_type: str
    trust: ArtifactTrust


class ArtifactPort(Protocol):
    def get_snapshot(
        self,
        requirement_id: str,
        artifact_id: str,
        artifact_version: str,
    ) -> ArtifactSnapshot: ...


class GatePolicySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    default_reviewer_id: str
    policy_code: str = "REQUIREMENT_BASELINE_WORKSPACE_OWNER"
    snapshot_hash: str = "sha256:bdfadcc2d2c32fdb9fdf327d45a231cd2e5cb9bf3028f4e09d527fdb50dd8ea2"


class GatePolicyPort(Protocol):
    def requirement_baseline(self, *, workspace_id: str) -> GatePolicySnapshot: ...


class GateReviewerGuardPort(Protocol):
    def can_decide(self, *, actor_id: str, workspace_id: str) -> bool: ...


class IntegrationBaselineEvidenceWorkItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_item_id: str
    repository_id: str
    task_commit_sha: str
    integration_merge_commit_sha: str
    artifact_references: tuple[ArtifactEvidenceReference, ...]


class IntegrationBaselineEvidenceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    evidence_hash: str
    delivery_snapshot_id: str
    delivery_snapshot_hash: str
    requirement_id: str
    requirement_version: int = Field(ge=1)
    required_work_item_set_version: int = Field(ge=1)
    required_work_item_set_hash: str
    currentness_state: Literal["CURRENT", "STALE", "UNAVAILABLE"]
    currentness_reasons: tuple[str, ...]
    work_items: tuple[IntegrationBaselineEvidenceWorkItem, ...]
    generated_at: datetime


class IntegrationBaselineEvidencePort(Protocol):
    def get(self, evidence_id: str) -> IntegrationBaselineEvidenceSnapshot: ...

    def get_by_snapshot(
        self, *, delivery_snapshot_id: str, delivery_snapshot_hash: str
    ) -> IntegrationBaselineEvidenceSnapshot: ...


class DeliveryGatePolicySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int = Field(ge=1)
    default_reviewer_id: str
    policy_code: str
    snapshot_hash: str
    resolution_snapshot: dict[str, object]

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        gate_type = (
            "REQUIREMENT_ACCEPTANCE"
            if self.policy_code == "REQUIREMENT_ACCEPTANCE_CREATOR"
            else "FORMAL_MR_REVIEW"
        )
        return frozen_gate_capabilities(self.resolution_snapshot, gate_type)


def frozen_gate_capabilities(
    resolution: dict[str, object],
    gate_type: str,
    *,
    expected_version: int | None = None,
    expected_hash: str | None = None,
) -> tuple[str, ...]:
    envelope = resolution.get("policy")
    if not isinstance(envelope, dict) or not isinstance(envelope.get("policy"), dict):
        raise ValueError("Frozen Gate policy is unavailable")
    values = envelope["policy"]
    if (
        type(envelope.get("version")) is not int
        or envelope["version"] < 1
        or (expected_version is not None and envelope["version"] != expected_version)
        or (
            expected_hash is not None and f"sha256:{envelope.get('snapshot_hash')}" != expected_hash
        )
    ):
        raise ValueError("Frozen Gate policy identity is invalid")
    policy = GatePolicy.parse(
        {
            "acceptance.additional_required_capabilities": values.get("acceptance_additional"),
            "formal_review.additional_required_capabilities": values.get(
                "formal_review_additional"
            ),
            "draft_archive_after_days": values.get("draft_archive_after_days"),
        },
        namespace=envelope["namespace"],
        scope=envelope["scope"],
        schema_revision=envelope["schema_revision"],
    )
    # Compare JSON shapes because stored tuples deserialize as lists.
    import json

    if content_hash(policy.values()) != envelope.get("snapshot_hash"):
        raise ValueError("Frozen Gate policy content hash is invalid")

    if json.dumps(values, sort_keys=True) != json.dumps(asdict(policy), sort_keys=True):
        raise ValueError("Frozen Gate system constraints are invalid")
    if gate_type == "REQUIREMENT_ACCEPTANCE":
        return policy.acceptance_required_capabilities
    if gate_type == "FORMAL_MR_REVIEW":
        return policy.formal_review_required_capabilities
    raise ValueError("Unsupported delivery Gate")


class DeliveryGatePolicyPort(Protocol):
    def requirement_acceptance(
        self,
        *,
        workspace_id: str,
        requirement_created_by: str,
    ) -> DeliveryGatePolicySnapshot: ...


class DeliveryReviewerEligibilitySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: bool
    actor_id: str
    required_capabilities: tuple[str, ...]
    workspace_id: str
    account_version: int | None
    workspace_version: int | None
    principal_version: int | None
    snapshot_hash: str
    details: dict[str, object]


class DeliveryReviewerGuardPort(Protocol):
    def evaluate(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        required_capabilities: tuple[str, ...],
    ) -> DeliveryReviewerEligibilitySnapshot: ...
