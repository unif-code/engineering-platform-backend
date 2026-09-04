"""The two supported policy owners; unknown or unconfigured owners fail closed."""

from dataclasses import dataclass

from control_plane.app.modules.configuration.domain import PolicySnapshotUnavailable
from control_plane.app.modules.configuration.ports.policy_runtime import PolicyRuntimePort


@dataclass(frozen=True, slots=True)
class PolicyRuntimeRegistry:
    identity: PolicyRuntimePort
    requirement_gate: PolicyRuntimePort | None = None

    def resolve(self, namespace: str) -> PolicyRuntimePort:
        if namespace == "identity":
            return self.identity
        if namespace == "requirement.gate" and self.requirement_gate is not None:
            return self.requirement_gate
        raise PolicySnapshotUnavailable("Policy owner unavailable")
