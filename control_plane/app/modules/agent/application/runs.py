import hashlib
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.errors import InvalidRequirementExecutionContext
from control_plane.app.modules.agent.application.idempotency import (
    IdempotencyConflict as IdempotencyConflict,
)
from control_plane.app.modules.agent.application.idempotency import (
    IdempotencyInProgress as IdempotencyInProgress,
)
from control_plane.app.modules.agent.application.idempotency import (
    execute_agent_command,
)
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentAuditAppend,
    AgentRun,
    AgentRunBusinessContext,
    AttemptState,
    CanonicalEventInput,
    EventAcceptanceReceipt,
    ExecutionBinding,
    RunState,
    WorkflowCommand,
    WorkflowCommandKind,
    WorkflowCommandState,
    transition_attempt,
)
from control_plane.app.modules.agent.domain.types import PlatformReference, PlatformUUID
from control_plane.app.modules.agent.ports import AgentUnitOfWork
from control_plane.app.modules.agent.ports.runtime import (
    ExecutionBindingRequest,
    RequirementExecutionContext,
    RequirementExecutionRequest,
    ResolvedActorReference,
)

_OPERATION = "agent.run.start"


class StartRunCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: PlatformUUID
    requirement_id: PlatformUUID
    work_item_id: PlatformUUID
    definition_id: PlatformUUID
    definition_version: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=10_000)
    actor: PlatformReference
    idempotency_key: str = Field(min_length=1)
    correlation_id: PlatformReference


class StartRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: AgentRun
    attempt: AgentAttempt
    binding: ExecutionBinding
    event: CanonicalEventInput
    command: WorkflowCommand


class DefinitionUnavailable(ValueError):
    pass


def _scope_actor(command: StartRunCommand, actor: ResolvedActorReference) -> str:
    return f"workspace:{command.workspace_id}:actor:{actor.reference}"


def _audit_reference(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def start_run(command: StartRunCommand, *, dependencies: AgentDependencies) -> StartRunResult:
    command = StartRunCommand.model_validate(command.model_dump(mode="python"))
    actor = dependencies.actor_resolver.resolve(command.actor)
    now = dependencies.clock()

    def operation(uow: AgentUnitOfWork) -> StartRunResult:
        repository = uow.repository()
        context = dependencies.requirement_context.resolve(
            RequirementExecutionRequest(
                workspace_id=command.workspace_id,
                requirement_id=command.requirement_id,
                work_item_id=command.work_item_id,
            )
        )
        try:
            context = RequirementExecutionContext.model_validate(context.model_dump(mode="python"))
            workspace_id = str(UUID(context.workspace_id))
            business_context = AgentRunBusinessContext(
                requirement_id=context.requirement_id,
                work_item_id=context.work_item_id,
                assignment_id=context.assignment_id,
            )
        except ValueError:
            raise InvalidRequirementExecutionContext(
                "Requirement context identities are invalid"
            ) from None
        if (
            workspace_id != command.workspace_id
            or business_context.requirement_id != command.requirement_id
            or business_context.work_item_id != command.work_item_id
        ):
            raise InvalidRequirementExecutionContext(
                "Requirement context identities do not match Agent request"
            )
        definition = repository.definition_by_id(command.definition_id, command.definition_version)
        if definition is None:
            raise DefinitionUnavailable("active Agent Definition is unavailable")
        if not dependencies.definition_availability.is_active(definition):
            raise DefinitionUnavailable("inactive Agent Definition is unavailable")
        binding = dependencies.binding_policy.resolve(
            ExecutionBindingRequest(
                binding_id=dependencies.new_id(), definition=definition, context=context
            )
        )

        run_id = dependencies.new_id()
        attempt_id = dependencies.new_id()
        initial = AgentAttempt(
            id=attempt_id,
            run_id=run_id,
            number=1,
            state=AttemptState.CREATED,
            binding_id=binding.id,
            binding_digest=binding.digest,
            runner_generation=1,
            fencing_token=f"fence:{attempt_id}:generation:1",
            checkpoint=None,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        binding_state = transition_attempt(initial, AttemptState.BINDING, checkpoint=None, now=now)
        queued = transition_attempt(
            binding_state, AttemptState.QUEUED, checkpoint=None, now=now
        ).model_copy(update={"event_sequence": 1})
        run = AgentRun(
            id=run_id,
            workspace_id=workspace_id,
            business_context=business_context,
            goal_ref=context.goal_ref,
            created_by=actor.reference,
            definition_id=definition.id,
            definition_version=definition.version,
            latest_attempt_id=attempt_id,
            state=RunState.ACTIVE,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        event = CanonicalEventInput(
            id=dependencies.new_id(),
            event_type="ATTEMPT_QUEUED",
            attempt_id=attempt_id,
            generation=1,
            sequence=1,
            correlation_id=command.correlation_id,
            causation_id=None,
            trace_id=f"agent-start:{run_id}",
            span_id=f"agent-start:{attempt_id}",
            summary="Attempt queued for restricted DEV control-plane execution",
            data={"bindingSource": binding.source.value, "bindingDigest": binding.digest},
        )
        workflow = WorkflowCommand(
            id=dependencies.new_id(),
            command_key=f"start:{attempt_id}:generation-1",
            kind=WorkflowCommandKind.START,
            attempt_id=attempt_id,
            generation=1,
            state=WorkflowCommandState.PLANNED,
            created_at=now,
            updated_at=now,
        )
        persisted_run = repository.insert_run(run)
        persisted_attempt = repository.insert_attempt(queued)
        persisted_binding = repository.insert_binding(attempt_id, binding)
        persisted_event = repository.append_event(event)
        repository.append_event_receipt(
            EventAcceptanceReceipt(
                event_id=persisted_event.id,
                attempt=persisted_attempt,
                checkpoint=None,
            )
        )
        persisted_workflow = repository.insert_workflow_command(workflow)
        result = StartRunResult(
            run=persisted_run,
            attempt=persisted_attempt,
            binding=persisted_binding,
            event=persisted_event,
            command=persisted_workflow,
        )
        uow.append_audit_event(
            AgentAuditAppend(
                id=dependencies.new_id(),
                occurred_at=now,
                actor=actor.reference,
                actor_type=actor.actor_type,
                action=_OPERATION,
                target_type="agent_run",
                target_id=run_id,
                result="QUEUED",
                reason=(
                    f"definition={definition.id}@{definition.version};"
                    f"bindingDigest={binding.digest};bindingSource={binding.source.value};"
                    f"requestRef={_audit_reference(command.correlation_id)};"
                    f"idempotencyRef={_audit_reference(command.idempotency_key)}"
                ),
                correlation_id=f"agent-correlation:{_audit_reference(command.correlation_id)}",
                schema_version=1,
            )
        )
        return result

    return dependencies.transaction_runner(
        lambda uow: execute_agent_command(
            repository=uow.repository(),
            dependencies=dependencies,
            actor=_scope_actor(command, actor),
            operation=_OPERATION,
            key=command.idempotency_key,
            path="/api/v1/agent-runs",
            body={
                key: value
                for key, value in command.model_dump(mode="json").items()
                if key not in {"actor", "idempotency_key", "correlation_id"}
            },
            result_type=StartRunResult,
            command=lambda: operation(uow),
        )
    )
