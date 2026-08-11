import pytest

import control_plane.app.modules.organization.domain as organization_domain


@pytest.mark.parametrize(
    ("superior_kind", "expected"),
    [(None, "MANAGER"), ("MANAGER", "LEADER"), ("LEADER", "MEMBER")],
)
def test_target_kind_is_derived_from_the_fixed_parent_level(
    superior_kind: str | None,
    expected: str,
) -> None:
    derive_kind = getattr(
        organization_domain,
        "derive_kind",
        lambda _superior_kind: None,
    )

    assert derive_kind(superior_kind) == expected


def test_member_cannot_be_a_superior() -> None:
    invalid_structure = getattr(organization_domain, "InvalidStructure", ValueError)
    derive_kind = getattr(
        organization_domain,
        "derive_kind",
        lambda _superior_kind: None,
    )

    with pytest.raises(invalid_structure):
        derive_kind("MEMBER")


@pytest.mark.parametrize(
    "edges",
    [
        {"manager": ("leader", "MANAGER"), "leader": ("manager", "LEADER")},
        {"leader-a": ("manager", "LEADER"), "leader-b": ("leader-a", "LEADER")},
        {"member": (None, "MEMBER")},
        {"member": ("leader", "MEMBER"), "child": ("member", "MEMBER")},
    ],
)
def test_invalid_cycles_and_fixed_level_expansion_are_rejected(
    edges: dict[str, tuple[str | None, str]],
) -> None:
    invalid_structure = getattr(organization_domain, "InvalidStructure", ValueError)
    validate_structure = getattr(
        organization_domain,
        "validate_structure",
        lambda _edges: None,
    )

    with pytest.raises(invalid_structure):
        validate_structure(edges)


def test_manager_leader_member_tree_is_valid() -> None:
    validate_structure = getattr(
        organization_domain,
        "validate_structure",
        lambda _edges: False,
    )

    assert (
        validate_structure(
            {
                "manager": (None, "MANAGER"),
                "leader": ("manager", "LEADER"),
                "member": ("leader", "MEMBER"),
            }
        )
        is None
    )
