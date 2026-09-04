import importlib
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def gate_policy() -> ModuleType:
    name = "control_plane.app.modules.requirement.domain.gate_policy"
    assert importlib.util.find_spec(name) is not None, "typed Gate policy is missing"
    return importlib.import_module(name)


def test_resolved_policy_keeps_security_floors_and_freezes_input(gate_policy: ModuleType) -> None:
    values: dict[str, Any] = {
        "acceptance.additional_required_capabilities": ["code.change"],
        "formal_review.additional_required_capabilities": [],
        "draft_archive_after_days": 30,
    }
    policy = gate_policy.GatePolicy.parse(
        values, namespace="requirement.gate", scope="PLATFORM", schema_revision=1
    )
    values["acceptance.additional_required_capabilities"].clear()
    assert policy.acceptance_required_capabilities == (
        "requirement.acceptance.decide",
        "code.change",
    )
    assert policy.formal_review_required_capabilities == ("merge_request.review",)
    assert policy.acceptance_default_route == "REQUIREMENT_CREATOR"
    assert policy.formal_review_default_routes == (("MEMBER", "DIRECT_LEADER"), ("LEADER", "SELF"))


def test_direct_constructor_cannot_hide_mutable_or_invalid_values(gate_policy: ModuleType) -> None:
    with pytest.raises(ValueError):
        gate_policy.GatePolicy(["code.change"], (), 30)
    with pytest.raises(ValueError):
        gate_policy.GatePolicy(("unknown",), (), 30)
    with pytest.raises(ValueError):
        gate_policy.GatePolicy((), (), True)


@pytest.mark.parametrize(
    "change",
    [
        {"acceptance.additional_required_capabilities": ["requirement.acceptance.decide"]},
        {"acceptance.additional_required_capabilities": ["code.change", "code.change"]},
        {"formal_review.additional_required_capabilities": "code.change"},
        {"draft_archive_after_days": True},
        {"draft_archive_after_days": 1.5},
        {"draft_archive_after_days": 0},
        {"draft_archive_after_days": 10**20},
        {"unknown": []},
    ],
)
def test_invalid_policy_cannot_weaken_floors_or_overflow_archive(
    change: dict[str, Any], gate_policy: ModuleType
) -> None:
    with pytest.raises(ValueError):
        gate_policy.GatePolicy.parse(
            {
                "acceptance.additional_required_capabilities": [],
                "formal_review.additional_required_capabilities": [],
                "draft_archive_after_days": 30,
                **change,
            },
            namespace="requirement.gate",
            scope="PLATFORM",
            schema_revision=1,
        )


@pytest.mark.parametrize(
    "namespace,scope,revision",
    [
        ("identity", "PLATFORM", 1),
        ("requirement.gate", "WORKSPACE", 1),
        ("requirement.gate", "PLATFORM", 2),
        ("requirement.gate", "PLATFORM", True),
    ],
)
def test_wrong_policy_identity_fails_closed(
    namespace: str, scope: str, revision: int, gate_policy: ModuleType
) -> None:
    with pytest.raises(ValueError):
        gate_policy.GatePolicy.parse({}, namespace=namespace, scope=scope, schema_revision=revision)
