"""Public organization facade; other modules must not import internals."""

from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.organization.application import (
    InvalidParticipant,
    OrganizationDependencies,
)
from control_plane.app.modules.organization.application.commands import (
    set_superior as _set_superior,
)
from control_plane.app.modules.organization.application.queries import (
    direct_reports as _direct_reports,
)
from control_plane.app.modules.organization.application.queries import get_tree as _get_tree
from control_plane.app.modules.organization.domain import (
    AccountRef,
    CorruptStructure,
    InvalidStructure,
    OrgKind,
    OrgTreeDto,
)


def set_superior(
    db: Connection,
    *,
    account_id: str,
    superior_id: str | None,
    actor: Any,
    reason: str,
    dependencies: OrganizationDependencies,
) -> None:
    _set_superior(
        dependencies.repository_factory(db),
        account_id=account_id,
        superior_id=superior_id,
        actor=actor,
        reason=reason,
        dependencies=dependencies,
    )


def get_tree(
    db: Connection,
    *,
    dependencies: OrganizationDependencies,
) -> OrgTreeDto:
    return _get_tree(dependencies.repository_factory(db), dependencies=dependencies)


def direct_reports(
    db: Connection,
    *,
    leader_id: str,
    dependencies: OrganizationDependencies,
) -> list[AccountRef]:
    return _direct_reports(
        dependencies.repository_factory(db),
        leader_id=leader_id,
        dependencies=dependencies,
    )


__all__ = [
    "AccountRef",
    "CorruptStructure",
    "InvalidParticipant",
    "InvalidStructure",
    "OrgKind",
    "OrganizationDependencies",
    "direct_reports",
    "get_tree",
    "set_superior",
]
