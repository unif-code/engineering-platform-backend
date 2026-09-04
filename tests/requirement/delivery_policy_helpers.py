from dataclasses import asdict

from control_plane.app.modules.requirement.domain.gate_policy import (
    GatePolicy,
    ResolvedGatePolicy,
    content_hash,
)


def resolved_policy(
    version: int, *, acceptance: tuple[str, ...] = (), formal: tuple[str, ...] = (), days: int = 30
) -> ResolvedGatePolicy:
    policy = GatePolicy(acceptance, formal, days)
    return ResolvedGatePolicy(
        "requirement.gate", "PLATFORM", 1, version, content_hash(policy.values()), policy
    )


def frozen_policy(version: int = 4) -> dict[str, object]:
    return {"policy": asdict(resolved_policy(version))}
