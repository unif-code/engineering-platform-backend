from datetime import datetime
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
