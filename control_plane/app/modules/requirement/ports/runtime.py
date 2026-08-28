from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.requirement.domain import RequirementType


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
