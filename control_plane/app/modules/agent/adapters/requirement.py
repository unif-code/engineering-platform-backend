from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection

from control_plane.app.modules.agent.application.errors import (
    AgentBusinessContextReason,
    InvalidRequirementExecutionContext,
)
from control_plane.app.modules.agent.ports.runtime import (
    RequirementExecutionContext,
    RequirementExecutionRequest,
)
from control_plane.app.modules.requirement import (
    RequirementDependencies,
    RequirementDetailsDto,
    assert_work_item_assignee_eligible,
    get_requirement,
    get_requirement_for_update,
)


class RequirementFacadeExecutionContext:
    """Resolve only public V0.4 Requirement facts into Agent platform references."""

    def __init__(self, db: Connection, dependencies: RequirementDependencies) -> None:
        self._db = db
        self._dependencies = dependencies

    def resolve(self, request: RequirementExecutionRequest) -> RequirementExecutionContext:
        details = get_requirement(
            self._db,
            requirement_id=request.requirement_id,
            dependencies=self._dependencies,
        )
        return self._context(request, details)

    @contextmanager
    def protect(
        self, request: RequirementExecutionRequest, *, expected_assignment_id: str
    ) -> Iterator[RequirementExecutionContext]:
        with self._db.begin():
            details = get_requirement_for_update(
                self._db,
                requirement_id=request.requirement_id,
                dependencies=self._dependencies,
            )
            current = self._context(request, details)
            if current.assignment_id != expected_assignment_id:
                raise InvalidRequirementExecutionContext(
                    "Current Assignment does not match the recorded source",
                    reason=AgentBusinessContextReason.ASSIGNMENT_CHANGED,
                )
            assert_work_item_assignee_eligible(
                self._db,
                requirement_id=current.requirement_id,
                work_item_id=current.work_item_id,
                expected_assignment_id=expected_assignment_id,
                dependencies=self._dependencies,
            )
            yield current

    def _context(
        self, request: RequirementExecutionRequest, details: RequirementDetailsDto
    ) -> RequirementExecutionContext:
        try:
            requirement_id = str(UUID(details.requirement.id))
            workspace_id = str(UUID(details.requirement.workspace_id))
            if requirement_id != request.requirement_id:
                raise ValueError("Requirement target changed")
            if workspace_id != request.workspace_id:
                raise InvalidRequirementExecutionContext(
                    "Requirement workspace does not match Agent workspace",
                    reason=AgentBusinessContextReason.WORKSPACE_CHANGED,
                )
            work_item_ids = []
            for item in details.work_items:
                work_item_ids.append(str(UUID(item.id)))
                if str(UUID(item.requirement_id)) != requirement_id:
                    raise InvalidRequirementExecutionContext(
                        "Requirement WorkItem does not belong to Requirement",
                        reason=AgentBusinessContextReason.OWNER_DATA_INVALID,
                    )
            assignments = []
            for assignment in details.work_item_assignments:
                assignment_id = str(UUID(assignment.id))
                work_item_id = str(UUID(assignment.work_item_id))
                if work_item_id not in work_item_ids or (
                    assignment.superseded_at is not None
                    and not isinstance(assignment.superseded_at, datetime)
                ):
                    raise ValueError("Assignment membership is inconsistent")
                if work_item_id == request.work_item_id and assignment.superseded_at is None:
                    assignments.append(assignment_id)
            matches = work_item_ids.count(request.work_item_id)
            if matches != 1:
                raise InvalidRequirementExecutionContext(
                    "Requirement WorkItem context is missing or ambiguous",
                    reason=(
                        AgentBusinessContextReason.WORK_ITEM_NOT_IN_REQUIREMENT
                        if matches == 0
                        else AgentBusinessContextReason.OWNER_DATA_AMBIGUOUS
                    ),
                )
            if len(assignments) != 1:
                raise InvalidRequirementExecutionContext(
                    "Requirement WorkItem has no current Agent assignment",
                    reason=(
                        AgentBusinessContextReason.ASSIGNMENT_MISSING
                        if not assignments
                        else AgentBusinessContextReason.OWNER_DATA_AMBIGUOUS
                    ),
                )
            return RequirementExecutionContext(
                workspace_id=workspace_id,
                requirement_id=requirement_id,
                work_item_id=request.work_item_id,
                assignment_id=assignments[0],
                goal_ref=f"requirement:{requirement_id}:work-item:{request.work_item_id}",
            )
        except InvalidRequirementExecutionContext:
            raise
        except (ValueError, TypeError, AttributeError):
            raise InvalidRequirementExecutionContext(
                "Requirement owner data is invalid",
                reason=AgentBusinessContextReason.OWNER_DATA_INVALID,
            ) from None
