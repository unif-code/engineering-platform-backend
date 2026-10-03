from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.agent.domain import AgentDefinition, ExecutionBinding


class RequirementExecutionRequest(BaseModel):
    """Stable platform identifiers used to resolve governed Requirement context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str
    requirement_id: str
    work_item_id: str


class ResolvedActorReference(BaseModel):
    """Platform-owned accountable identity resolved from untrusted caller input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    actor_type: Literal["EMPLOYEE", "SERVICE", "SYSTEM"]


class RequirementExecutionContext(BaseModel):
    """Requirement facts copied across the module boundary without source text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str
    requirement_id: str
    work_item_id: str
    assignment_id: str
    goal_ref: str


class ExecutionBindingRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_id: str
    definition: AgentDefinition
    context: RequirementExecutionContext


class RequirementExecutionContextPort(Protocol):
    def resolve(self, request: RequirementExecutionRequest) -> RequirementExecutionContext: ...

    def protect(
        self, request: RequirementExecutionRequest, *, expected_assignment_id: str
    ) -> AbstractContextManager[RequirementExecutionContext]: ...


class ExecutionBindingPolicyPort(Protocol):
    def resolve(self, request: ExecutionBindingRequest) -> ExecutionBinding: ...


class DefinitionAvailabilityPort(Protocol):
    """Platform-owned availability projection, isolated from future Configuration."""

    def is_active(self, definition: AgentDefinition) -> bool: ...


class ActorResolverPort(Protocol):
    def resolve(self, untrusted_actor: str) -> ResolvedActorReference: ...


class EventCursorCodecPort(Protocol):
    """Integrity-protects opaque, run-scoped canonical-event cursors."""

    def encode(self, *, run_id: str, event_id: str) -> str: ...

    def decode(self, *, run_id: str, cursor: str) -> str: ...


Clock = Callable[[], datetime]
NewId = Callable[[], str]
