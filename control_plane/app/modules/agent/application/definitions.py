import hashlib

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.domain import AgentAuditAppend, AgentDefinition
from control_plane.app.modules.agent.domain.types import (
    PlatformName,
    PlatformReference,
    PlatformUUID,
)
from control_plane.app.modules.agent.ports import AgentUnitOfWork


class RegisterDefinitionCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: PlatformUUID
    version: int = Field(ge=1)
    name: PlatformName
    capability_declarations: tuple[PlatformName, ...]
    skill_declarations: tuple[PlatformName, ...]
    runtime_permissions: tuple[PlatformName, ...]
    input_schema: dict[str, object]
    actor: PlatformReference
    correlation_id: PlatformReference


class DefinitionAlreadyExists(ValueError):
    pass


def register_definition(
    command: RegisterDefinitionCommand, *, dependencies: AgentDependencies
) -> AgentDefinition:
    command = RegisterDefinitionCommand.model_validate(command.model_dump(mode="python"))
    actor = dependencies.actor_resolver.resolve(command.actor)
    now = dependencies.clock()

    def operation(uow: AgentUnitOfWork) -> AgentDefinition:
        repository = uow.repository()
        existing = repository.definition_by_id(command.id, command.version)
        if existing is not None:
            raise DefinitionAlreadyExists(f"Agent Definition {command.id}@{command.version} exists")
        definition = AgentDefinition(
            id=command.id,
            version=command.version,
            name=command.name,
            capability_declarations=command.capability_declarations,
            skill_declarations=command.skill_declarations,
            runtime_permissions=command.runtime_permissions,
            input_schema=command.input_schema,
            created_at=now,
        )
        persisted = repository.insert_definition(definition)
        uow.append_audit_event(
            AgentAuditAppend(
                id=dependencies.new_id(),
                occurred_at=now,
                actor=actor.reference,
                actor_type=actor.actor_type,
                action="agent.definition.register",
                target_type="agent_definition",
                target_id=f"{definition.id}@{definition.version}",
                result="SUCCESS",
                reason=f"definition={definition.id}@{definition.version}",
                correlation_id=(
                    "agent-correlation:"
                    + hashlib.sha256(command.correlation_id.encode()).hexdigest()
                ),
                schema_version=1,
            )
        )
        return persisted

    return dependencies.transaction_runner(operation)


def list_definitions(*, dependencies: AgentDependencies) -> tuple[AgentDefinition, ...]:
    def operation(uow: AgentUnitOfWork) -> tuple[AgentDefinition, ...]:
        return tuple(
            definition
            for definition in uow.repository().list_definitions()
            if dependencies.definition_availability.is_active(definition)
        )

    return dependencies.transaction_runner(operation)
