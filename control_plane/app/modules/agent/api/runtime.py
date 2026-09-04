from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection, Engine

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.ports.runtime import (
    RequirementExecutionContext,
    RequirementExecutionContextPort,
    RequirementExecutionRequest,
    ResolvedActorReference,
)

RequirementContextFactory = Callable[[Connection], RequirementExecutionContextPort]


@dataclass(frozen=True, slots=True)
class AgentHttpRuntime:
    """Cached factories and engines only; no live Requirement connection is retained."""

    engine: Engine
    dependencies: AgentDependencies
    requirement_engine: Engine
    requirement_context_factory: RequirementContextFactory


class UnboundRequirementExecutionContext:
    """Fails closed until an HTTP request binds its live Requirement connection."""

    def resolve(self, _request: RequirementExecutionRequest) -> RequirementExecutionContext:
        raise RuntimeError("request-scoped Requirement context is unavailable")


class CurrentPrincipalActorResolver:
    """Trust exactly the employee reference established by the current session."""

    def __init__(self, employee_id: str) -> None:
        if not employee_id:
            raise ValueError("current Principal employee reference is unavailable")
        self._employee_id = employee_id

    def resolve(self, untrusted_actor: str) -> ResolvedActorReference:
        if untrusted_actor != self._employee_id:
            raise ValueError("actor does not match the current Principal")
        return ResolvedActorReference(reference=self._employee_id, actor_type="EMPLOYEE")
