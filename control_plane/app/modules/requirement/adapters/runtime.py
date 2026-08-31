from control_plane.app.modules.requirement.domain import (
    RequirementType,
    canonical_route_snapshot_hash,
)
from control_plane.app.modules.requirement.ports import RouteSnapshot


class V03RouteSnapshotCatalog:
    """Code-owned first-batch delivery routes frozen into each Requirement."""

    _VERSION = 1
    _CAPABILITIES = ("code.change",)

    def current(self, requirement_type: RequirementType) -> RouteSnapshot:
        payload = {
            "requirementType": requirement_type.value,
            "requiredCapabilities": list(self._CAPABILITIES),
            "version": self._VERSION,
        }
        return RouteSnapshot(
            version=self._VERSION,
            snapshot_hash=canonical_route_snapshot_hash(payload),
            required_capabilities=self._CAPABILITIES,
            requirement_type=requirement_type,
        )


class V04RouteSnapshotCatalog:
    """Code-owned V0.4 delivery routes frozen into each Requirement."""

    _VERSION = 2
    _CAPABILITIES = ("code.change",)
    _STEPS = {
        RequirementType.FEAT: (
            "brainstorming",
            "writing-plans",
            "test-driven-development",
            "verification-before-completion",
            "requesting-code-review",
        ),
        RequirementType.FIX: (
            "systematic-debugging",
            "test-driven-development",
            "verification-before-completion",
            "requesting-code-review",
        ),
        RequirementType.REFACTOR: (
            "writing-plans",
            "test-driven-development",
            "verification-before-completion",
            "requesting-code-review",
        ),
        RequirementType.CHORE: (
            "writing-plans",
            "test-driven-development",
            "verification-before-completion",
            "requesting-code-review",
        ),
    }

    def current(self, requirement_type: RequirementType) -> RouteSnapshot:
        steps = self._STEPS[requirement_type]
        payload = {
            "requirementType": requirement_type.value,
            "requiredCapabilities": list(self._CAPABILITIES),
            "steps": list(steps),
            "version": self._VERSION,
        }
        return RouteSnapshot(
            version=self._VERSION,
            snapshot_hash=canonical_route_snapshot_hash(payload),
            required_capabilities=self._CAPABILITIES,
            requirement_type=requirement_type,
            steps=steps,
        )


class FailClosedAutomaticAssignmentGuard:
    """Keep WorkItems unassigned until repository-scoped eligibility is integrated."""

    def can_auto_assign(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: str,
        required_capabilities: tuple[str, ...],
    ) -> bool:
        del actor_id, workspace_id, repository_id, required_capabilities
        return False
