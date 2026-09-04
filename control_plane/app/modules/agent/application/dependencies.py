from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from control_plane.app.modules.agent.ports import AgentTransactionRunner
from control_plane.app.modules.agent.ports.runtime import (
    ActorResolverPort,
    Clock,
    DefinitionAvailabilityPort,
    EventCursorCodecPort,
    ExecutionBindingPolicyPort,
    NewId,
    RequirementExecutionContextPort,
)
from control_plane.app.shared.security import SecretManagerPort

if TYPE_CHECKING:
    from control_plane.app.modules.agent.application.workflow import WorkflowOrchestratorPort


@dataclass(frozen=True, slots=True)
class AgentDependencies:
    transaction_runner: AgentTransactionRunner
    requirement_context: RequirementExecutionContextPort
    binding_policy: ExecutionBindingPolicyPort
    definition_availability: DefinitionAvailabilityPort
    actor_resolver: ActorResolverPort
    clock: Clock
    new_id: NewId
    cursor_codec: EventCursorCodecPort
    secret_manager: SecretManagerPort
    workflow_orchestrator: WorkflowOrchestratorPort | None = None

    def with_requirement_context(
        self, requirement_context: RequirementExecutionContextPort
    ) -> AgentDependencies:
        return replace(self, requirement_context=requirement_context)

    def with_actor_resolver(self, actor_resolver: ActorResolverPort) -> AgentDependencies:
        return replace(self, actor_resolver=actor_resolver)
