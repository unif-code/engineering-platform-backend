from sqlalchemy import Connection

from control_plane.app.modules.agent.application.errors import InvalidRequirementExecutionContext
from control_plane.app.modules.agent.ports.runtime import (
    RequirementExecutionContext,
    RequirementExecutionRequest,
)
from control_plane.app.modules.requirement import RequirementDependencies, get_requirement


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
        requirement = details.requirement
        if requirement.workspace_id != request.workspace_id:
            raise InvalidRequirementExecutionContext(
                "Requirement workspace does not match Agent workspace"
            )
        work_items = [item for item in details.work_items if item.id == request.work_item_id]
        if len(work_items) != 1:
            raise InvalidRequirementExecutionContext(
                "Requirement WorkItem context is missing or ambiguous"
            )
        if work_items[0].requirement_id != requirement.id:
            raise InvalidRequirementExecutionContext(
                "Requirement WorkItem does not belong to Requirement"
            )
        assignments = [
            assignment
            for assignment in details.work_item_assignments
            if assignment.work_item_id == request.work_item_id and assignment.superseded_at is None
        ]
        if len(assignments) != 1:
            raise InvalidRequirementExecutionContext(
                "Requirement WorkItem has no current Agent assignment"
            )
        return RequirementExecutionContext(
            workspace_id=requirement.workspace_id,
            requirement_id=requirement.id,
            work_item_id=work_items[0].id,
            assignment_id=assignments[0].id,
            goal_ref=f"requirement:{requirement.id}:work-item:{work_items[0].id}",
        )
