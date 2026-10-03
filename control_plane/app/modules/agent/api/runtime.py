from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass

from sqlalchemy import Connection, Engine
from starlette.exceptions import HTTPException

from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.errors import InvalidRequirementExecutionContext
from control_plane.app.modules.agent.ports.runtime import (
    RequirementExecutionContext,
    RequirementExecutionContextPort,
    RequirementExecutionRequest,
    ResolvedActorReference,
)
from control_plane.app.modules.requirement import (
    RequirementDependencyUnavailable,
    RequirementNotFound,
    WorkItemAssigneeIneligible,
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

    def protect(
        self, _request: RequirementExecutionRequest, *, expected_assignment_id: str
    ) -> AbstractContextManager[RequirementExecutionContext]:
        raise RequirementDependencyUnavailable(
            "request-scoped Requirement protection is unavailable"
        )


class LazyRequirementExecutionContext(UnboundRequirementExecutionContext):
    """Open owner resources only when a new resume mutation requests protection."""

    def __init__(self, engine: Engine, factory: RequirementContextFactory) -> None:
        self._engine = engine
        self._factory = factory

    @contextmanager
    def protect(
        self, request: RequirementExecutionRequest, *, expected_assignment_id: str
    ) -> Iterator[RequirementExecutionContext]:
        with ExitStack() as resources:
            try:
                db = resources.enter_context(self._engine.connect())
                current = resources.enter_context(
                    self._factory(db).protect(
                        request, expected_assignment_id=expected_assignment_id
                    )
                )
            except (
                RequirementNotFound,
                InvalidRequirementExecutionContext,
                RequirementDependencyUnavailable,
                WorkItemAssigneeIneligible,
            ):
                raise
            except HTTPException as error:
                if error.status_code in (401, 403):
                    raise
                raise RequirementDependencyUnavailable(
                    "Requirement protection is unavailable"
                ) from None
            except Exception:
                raise RequirementDependencyUnavailable(
                    "Requirement protection is unavailable"
                ) from None
            yield current


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
