import pytest

from control_plane.app.modules.authorization import (
    Scope,
    is_v02_super_admin_platform_capability,
)


@pytest.mark.parametrize(
    "capability",
    [
        "platform.organization.read",
        "platform.workspace.read",
    ],
)
def test_super_admin_can_read_v02_admin_screen_data(capability: str) -> None:
    assert is_v02_super_admin_platform_capability(
        capability,
        Scope.platform(),
        is_super_admin=True,
    )


@pytest.mark.parametrize(
    ("scope", "is_super_admin"),
    [
        (Scope.platform(), False),
        (Scope.workspace("workspace-1"), True),
    ],
)
def test_v02_read_capabilities_do_not_bypass_scope_or_admin_fact(
    scope: Scope,
    is_super_admin: bool,
) -> None:
    for capability in (
        "platform.organization.read",
        "platform.workspace.read",
    ):
        assert not is_v02_super_admin_platform_capability(
            capability,
            scope,
            is_super_admin=is_super_admin,
        )
