"""Public Agent Facade; callers never reach Agent internals."""

from typing import Any

from control_plane.app.modules.agent.application.control import (
    AttemptControlResult,
    CancelAttemptCommand,
    ResumeAttemptCommand,
)
from control_plane.app.modules.agent.application.control import (
    cancel_attempt as _cancel_attempt,
)
from control_plane.app.modules.agent.application.control import (
    resume_attempt as _resume_attempt,
)
from control_plane.app.modules.agent.application.definitions import (
    RegisterDefinitionCommand,
)
from control_plane.app.modules.agent.application.definitions import (
    list_definitions as _list_definitions,
)
from control_plane.app.modules.agent.application.definitions import (
    register_definition as _register_definition,
)
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.events import (
    EventAcceptance,
    EventReplayUnavailable,
)
from control_plane.app.modules.agent.application.events import (
    accept_workflow_event as _accept_workflow_event,
)
from control_plane.app.modules.agent.application.idempotency import AgentReplayUnavailable
from control_plane.app.modules.agent.application.queries import (
    AgentRunBusinessContextStatus,
    AgentRunPage,
    AgentRunView,
    CanonicalEventPage,
)
from control_plane.app.modules.agent.application.queries import (
    get_business_context_status as _get_business_context_status,
)
from control_plane.app.modules.agent.application.queries import (
    get_run as _get_run,
)
from control_plane.app.modules.agent.application.queries import (
    get_run_metadata as _get_run_metadata,
)
from control_plane.app.modules.agent.application.queries import (
    list_events as _list_events,
)
from control_plane.app.modules.agent.application.queries import list_runs as _list_runs
from control_plane.app.modules.agent.application.runs import (
    StartRunCommand,
    StartRunResult,
)
from control_plane.app.modules.agent.application.runs import (
    start_run as _start_run,
)
from control_plane.app.modules.agent.application.workflow import (
    WorkflowDispatchResult,
    WorkflowReconciliationResult,
)
from control_plane.app.modules.agent.application.workflow import (
    dispatch_workflow_commands as _dispatch_workflow_commands,
)
from control_plane.app.modules.agent.application.workflow import (
    reconcile_workflow_commands as _reconcile_workflow_commands,
)
from control_plane.app.modules.agent.domain import (
    AgentDefinition,
    AgentRun,
    CanonicalEventInput,
    RunState,
)


def register_definition(
    db: Any, *, command: RegisterDefinitionCommand, dependencies: AgentDependencies
) -> AgentDefinition:
    del db
    return _register_definition(command, dependencies=dependencies)


def list_definitions(db: Any, *, dependencies: AgentDependencies) -> tuple[AgentDefinition, ...]:
    del db
    return _list_definitions(dependencies=dependencies)


def start_run(
    db: Any, *, command: StartRunCommand, dependencies: AgentDependencies
) -> StartRunResult:
    del db
    return _start_run(command, dependencies=dependencies)


def accept_workflow_event(
    db: Any, *, event: CanonicalEventInput, dependencies: AgentDependencies
) -> EventAcceptance:
    del db
    return _accept_workflow_event(event, dependencies=dependencies)


def cancel_attempt(
    db: Any, *, command: CancelAttemptCommand, dependencies: AgentDependencies
) -> AttemptControlResult:
    del db
    return _cancel_attempt(command, dependencies=dependencies)


def resume_attempt(
    db: Any, *, command: ResumeAttemptCommand, dependencies: AgentDependencies
) -> AttemptControlResult:
    del db
    return _resume_attempt(command, dependencies=dependencies)


def get_run(db: Any, *, run_id: str, dependencies: AgentDependencies) -> AgentRunView:
    del db
    return _get_run(run_id, dependencies=dependencies)


def get_run_metadata(db: Any, *, run_id: str, dependencies: AgentDependencies) -> AgentRun:
    del db
    return _get_run_metadata(run_id, dependencies=dependencies)


def get_business_context_status(
    db: Any, *, run: AgentRun, dependencies: AgentDependencies
) -> AgentRunBusinessContextStatus:
    del db
    return _get_business_context_status(run, dependencies=dependencies)


def list_runs(
    db: Any,
    *,
    workspace_id: str,
    state: RunState | None,
    cursor: str | None,
    limit: int,
    dependencies: AgentDependencies,
) -> AgentRunPage:
    del db
    return _list_runs(
        workspace_id, state=state, cursor=cursor, limit=limit, dependencies=dependencies
    )


def list_events(
    db: Any,
    *,
    run_id: str,
    cursor: str | None,
    limit: int,
    dependencies: AgentDependencies,
) -> CanonicalEventPage:
    del db
    return _list_events(run_id, cursor=cursor, limit=limit, dependencies=dependencies)


def dispatch_workflow_commands(
    db: Any, *, limit: int, dependencies: AgentDependencies
) -> WorkflowDispatchResult:
    del db
    return _dispatch_workflow_commands(limit=limit, dependencies=dependencies)


def reconcile_workflow_commands(
    db: Any, *, limit: int, dependencies: AgentDependencies
) -> WorkflowReconciliationResult:
    del db
    return _reconcile_workflow_commands(limit=limit, dependencies=dependencies)


__all__ = [
    "AgentReplayUnavailable",
    "EventReplayUnavailable",
    "accept_workflow_event",
    "dispatch_workflow_commands",
    "cancel_attempt",
    "get_run",
    "get_run_metadata",
    "get_business_context_status",
    "list_runs",
    "list_definitions",
    "list_events",
    "register_definition",
    "reconcile_workflow_commands",
    "resume_attempt",
    "start_run",
]
