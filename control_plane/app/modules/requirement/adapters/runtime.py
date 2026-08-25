import hashlib
import json

from control_plane.app.modules.requirement.domain import RequirementType
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
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return RouteSnapshot(
            version=self._VERSION,
            snapshot_hash=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            required_capabilities=self._CAPABILITIES,
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
