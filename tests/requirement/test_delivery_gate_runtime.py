from dataclasses import asdict
from types import SimpleNamespace

import pytest

from control_plane.app.modules.requirement.domain.gate_policy import (
    GatePolicy,
    ResolvedGatePolicy,
    content_hash,
)


def test_acceptance_routes_creator_and_freezes_full_policy_after_publication() -> None:
    from control_plane.app.modules.requirement import adapters

    adapter_type = getattr(adapters, "DeliveryGatePolicyAdapter", None)
    assert adapter_type is not None, "Delivery Gate must consume the owner policy runtime"
    policy = GatePolicy(("code.change",), (), 30)
    runtime = SimpleNamespace(
        resolved_snapshot=lambda: ResolvedGatePolicy(
            "requirement.gate", "PLATFORM", 1, 2, content_hash(policy.values()), policy
        )
    )
    adapter = adapter_type(runtime)
    old = adapter.requirement_acceptance(workspace_id="w", requirement_created_by="creator")
    runtime.resolved_snapshot = lambda: ResolvedGatePolicy(
        "requirement.gate",
        "PLATFORM",
        1,
        3,
        content_hash(GatePolicy((), (), 60).values()),
        GatePolicy((), (), 60),
    )
    fresh = adapter.requirement_acceptance(workspace_id="w", requirement_created_by="creator")
    assert old.default_reviewer_id == "creator"
    assert old.version == 2 and fresh.version == 3
    assert old.resolution_snapshot["policy"]["policy"] == asdict(policy)
    assert old.required_capabilities == ("requirement.acceptance.decide", "code.change")
    assert fresh.required_capabilities == ("requirement.acceptance.decide",)


def test_frozen_gate_policy_requires_additional_capability_and_rejects_missing_snapshot() -> None:
    import control_plane.app.modules.requirement.ports.runtime as runtime

    resolver = getattr(runtime, "frozen_gate_capabilities", None)
    assert resolver is not None, "Decisions must load capabilities from the frozen assignment"
    policy = GatePolicy(("code.change",), (), 30)
    envelope = asdict(
        ResolvedGatePolicy(
            "requirement.gate", "PLATFORM", 1, 2, content_hash(policy.values()), policy
        )
    )
    assert resolver({"policy": envelope}, "REQUIREMENT_ACCEPTANCE") == (
        "requirement.acceptance.decide",
        "code.change",
    )
    with pytest.raises(ValueError):
        resolver({}, "REQUIREMENT_ACCEPTANCE")


def test_frozen_policy_rejects_tampered_capabilities() -> None:
    from control_plane.app.modules.requirement.ports.runtime import frozen_gate_capabilities

    policy = GatePolicy(("code.change",), (), 30)
    envelope = asdict(
        ResolvedGatePolicy(
            "requirement.gate", "PLATFORM", 1, 2, content_hash(policy.values()), policy
        )
    )
    envelope["policy"] = asdict(GatePolicy((), (), 30))
    with pytest.raises(ValueError):
        frozen_gate_capabilities({"policy": envelope}, "REQUIREMENT_ACCEPTANCE")
