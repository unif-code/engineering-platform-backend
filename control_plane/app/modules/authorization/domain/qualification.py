"""Immutable sessionless actor evidence; assignment remains consumer-owned."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.authorization.domain.models import GrantDto, PrincipalVersionDto


class ActorAccountFacts(BaseModel):
    model_config = ConfigDict(frozen=True)
    account_id: str
    version: int
    status: str
    initialized: bool


class ActorWorkspaceFacts(BaseModel):
    model_config = ConfigDict(frozen=True)
    workspace_id: str
    version: int
    member_source: str | None
    member_computed_at: datetime | None
    archived: bool


class ActorQualificationSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    eligible: bool
    reason: str
    actor_id: str
    workspace_id: str
    required_capabilities: tuple[str, ...]
    account: ActorAccountFacts | None
    workspace: ActorWorkspaceFacts | None
    principal: PrincipalVersionDto | None
    grants: tuple[GrantDto, ...]
    checked_at: datetime
    snapshot_hash: str
