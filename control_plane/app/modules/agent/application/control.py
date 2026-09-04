import hashlib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.idempotency import execute_agent_command
from control_plane.app.modules.agent.application.queries import AgentRunNotFound
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentAuditAppend,
    AttemptMutation,
    AttemptNotResumable,
    AttemptState,
    WorkflowCommand,
    WorkflowCommandKind,
    WorkflowCommandState,
    resume_generation,
    transition_attempt,
)
from control_plane.app.modules.agent.domain.types import PlatformReference, PlatformUUID
from control_plane.app.modules.agent.ports import AgentUnitOfWork
from control_plane.app.modules.agent.ports.runtime import ResolvedActorReference

_CANCEL_OPERATION = "agent.attempt.cancel"
_RESUME_OPERATION = "agent.attempt.resume"
_TERMINAL_STATES = {
    AttemptState.SUCCEEDED,
    AttemptState.FAILED,
    AttemptState.CANCELED,
    AttemptState.TIMED_OUT,
}


class AttemptRevisionConflict(ValueError):
    pass


class AttemptWaitingExpired(ValueError):
    pass


class BindingDigestMismatch(ValueError):
    pass


class AgentAttemptNotFound(LookupError):
    pass


class _AttemptControlCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: PlatformUUID
    attempt_id: PlatformUUID
    expected_revision: int = Field(ge=1)
    actor: PlatformReference
    idempotency_key: str = Field(min_length=1)
    correlation_id: PlatformReference


class CancelAttemptCommand(_AttemptControlCommand):
    pass


class ResumeAttemptCommand(_AttemptControlCommand):
    pass


class AttemptControlResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt: AgentAttempt
    command: WorkflowCommand | None


def _audit_reference(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope_actor(workspace_id: str, actor: ResolvedActorReference) -> str:
    return f"workspace:{workspace_id}:actor:{actor.reference}"


def _audit(
    uow: AgentUnitOfWork,
    *,
    operation: str,
    result: AttemptControlResult,
    command: _AttemptControlCommand,
    actor: ResolvedActorReference,
    now: datetime,
    dependencies: AgentDependencies,
) -> None:
    uow.append_audit_event(
        AgentAuditAppend(
            id=dependencies.new_id(),
            occurred_at=now,
            actor=actor.reference,
            actor_type=actor.actor_type,
            action=operation,
            target_type="agent_attempt",
            target_id=result.attempt.id,
            result=result.attempt.state.value,
            reason=(
                f"revision={result.attempt.revision};generation={result.attempt.runner_generation};"
                f"requestRef={_audit_reference(command.correlation_id)};"
                f"idempotencyRef={_audit_reference(command.idempotency_key)}"
            ),
            correlation_id=f"agent-correlation:{_audit_reference(command.correlation_id)}",
            schema_version=1,
        )
    )


def _mutation(attempt: AgentAttempt, *, now: datetime) -> AttemptMutation:
    return AttemptMutation(
        state=attempt.state,
        runner_generation=attempt.runner_generation,
        fencing_token=attempt.fencing_token,
        checkpoint=attempt.checkpoint,
        event_sequence=attempt.event_sequence,
        waiting_deadline=attempt.waiting_deadline,
        terminal_evidence=attempt.terminal_evidence,
        now=now,
    )


def cancel_attempt(
    command: CancelAttemptCommand, *, dependencies: AgentDependencies
) -> AttemptControlResult:
    command = CancelAttemptCommand.model_validate(command.model_dump(mode="python"))
    actor = dependencies.actor_resolver.resolve(command.actor)

    def operation(uow: AgentUnitOfWork) -> AttemptControlResult:
        repository = uow.repository()
        run = repository.run_by_id(command.run_id, for_update=True)
        if run is None:
            raise AgentRunNotFound(command.run_id)
        attempt = repository.attempt_by_id(command.attempt_id, for_update=True)
        if attempt is None or attempt.run_id != run.id:
            raise AgentAttemptNotFound(command.attempt_id)
        now = dependencies.clock()

        def mutate() -> AttemptControlResult:
            if attempt.revision != command.expected_revision:
                raise AttemptRevisionConflict(
                    "If-Match does not match the current Attempt revision"
                )
            workflow: WorkflowCommand | None = None
            persisted_attempt = attempt
            if (
                attempt.state not in _TERMINAL_STATES
                and attempt.state is not AttemptState.CANCELING
            ):
                canceling = transition_attempt(
                    attempt, AttemptState.CANCELING, checkpoint=None, now=now
                )
                updated_attempt = repository.compare_and_set_attempt(
                    attempt.id,
                    expected_revision=attempt.revision,
                    mutation=_mutation(canceling, now=now),
                )
                if updated_attempt is None:
                    raise RuntimeError("Attempt cancellation lost its row lock")
                persisted_attempt = updated_attempt
                workflow = repository.insert_workflow_command(
                    WorkflowCommand(
                        id=dependencies.new_id(),
                        command_key=(f"cancel:{attempt.id}:generation-{attempt.runner_generation}"),
                        kind=WorkflowCommandKind.CANCEL,
                        attempt_id=attempt.id,
                        generation=attempt.runner_generation,
                        state=WorkflowCommandState.PLANNED,
                        created_at=now,
                        updated_at=now,
                    )
                )
            result = AttemptControlResult(attempt=persisted_attempt, command=workflow)
            _audit(
                uow,
                operation=_CANCEL_OPERATION,
                result=result,
                command=command,
                actor=actor,
                now=now,
                dependencies=dependencies,
            )
            return result

        return execute_agent_command(
            repository=repository,
            dependencies=dependencies,
            actor=_scope_actor(run.workspace_id, actor),
            operation=_CANCEL_OPERATION,
            key=command.idempotency_key,
            path=f"/api/v1/agent-runs/{run.id}/attempts/{attempt.id}/cancel",
            body={"expectedRevision": command.expected_revision},
            result_type=AttemptControlResult,
            command=mutate,
        )

    return dependencies.transaction_runner(operation)


def resume_attempt(
    command: ResumeAttemptCommand, *, dependencies: AgentDependencies
) -> AttemptControlResult:
    command = ResumeAttemptCommand.model_validate(command.model_dump(mode="python"))
    actor = dependencies.actor_resolver.resolve(command.actor)

    def operation(uow: AgentUnitOfWork) -> AttemptControlResult:
        repository = uow.repository()
        run = repository.run_by_id(command.run_id, for_update=True)
        if run is None:
            raise AgentRunNotFound(command.run_id)
        attempt = repository.attempt_by_id(command.attempt_id, for_update=True)
        if attempt is None or attempt.run_id != run.id:
            raise AgentAttemptNotFound(command.attempt_id)
        now = dependencies.clock()

        def mutate() -> AttemptControlResult:
            if attempt.revision != command.expected_revision:
                raise AttemptRevisionConflict(
                    "If-Match does not match the current Attempt revision"
                )
            if attempt.state is not AttemptState.WAITING_INPUT or attempt.checkpoint is None:
                raise AttemptNotResumable(attempt.id)
            if attempt.waiting_deadline is None or attempt.waiting_deadline <= now:
                raise AttemptWaitingExpired(attempt.id)
            try:
                binding = repository.binding_by_attempt_id(attempt.id)
            except ValueError as error:
                raise BindingDigestMismatch(attempt.id) from error
            if (
                binding is None
                or binding.id != attempt.binding_id
                or binding.digest != attempt.binding_digest
            ):
                raise BindingDigestMismatch(attempt.id)
            resumed = resume_generation(
                attempt,
                fencing_token=f"fence:{attempt.id}:generation:{attempt.runner_generation + 1}",
                now=now,
            )
            persisted_attempt = repository.compare_and_set_attempt(
                attempt.id,
                expected_revision=attempt.revision,
                mutation=_mutation(resumed, now=now).model_copy(
                    update={"event_sequence": 0, "waiting_deadline": None}
                ),
            )
            if persisted_attempt is None:
                raise RuntimeError("Attempt resume lost its row lock")
            workflow = repository.insert_workflow_command(
                WorkflowCommand(
                    id=dependencies.new_id(),
                    command_key=f"resume:{attempt.id}:generation-{persisted_attempt.runner_generation}",
                    kind=WorkflowCommandKind.RESUME,
                    attempt_id=attempt.id,
                    generation=persisted_attempt.runner_generation,
                    state=WorkflowCommandState.PLANNED,
                    created_at=now,
                    updated_at=now,
                )
            )
            result = AttemptControlResult(attempt=persisted_attempt, command=workflow)
            _audit(
                uow,
                operation=_RESUME_OPERATION,
                result=result,
                command=command,
                actor=actor,
                now=now,
                dependencies=dependencies,
            )
            return result

        return execute_agent_command(
            repository=repository,
            dependencies=dependencies,
            actor=_scope_actor(run.workspace_id, actor),
            operation=_RESUME_OPERATION,
            key=command.idempotency_key,
            path=f"/api/v1/agent-runs/{run.id}/attempts/{attempt.id}/resume",
            body={"expectedRevision": command.expected_revision},
            result_type=AttemptControlResult,
            command=mutate,
        )

    return dependencies.transaction_runner(operation)
