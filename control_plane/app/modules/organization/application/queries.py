from typing import TypeGuard

from control_plane.app.modules.organization.application.dependencies import (
    OrganizationDependencies,
)
from control_plane.app.modules.organization.domain import (
    AccountRef,
    CorruptStructure,
    InvalidStructure,
    LeaderNode,
    ManagerNode,
    OrgKind,
    OrgTreeDto,
    validate_structure,
)
from control_plane.app.modules.organization.ports import (
    OrganizationAccountView,
    OrganizationRepository,
)


def _is_effective(
    account: OrganizationAccountView | None,
) -> TypeGuard[OrganizationAccountView]:
    if account is None:
        return False
    return (
        str(getattr(account, "status", None)) == "ENABLED"
        and getattr(account, "initialized", False) is True
    )


def _account_ref(account: OrganizationAccountView) -> AccountRef:
    return AccountRef(
        id=account.id,
        employee_no=account.employee_no,
        display_name=account.display_name,
    )


def _sort_key(account: AccountRef) -> tuple[str, str]:
    return account.employee_no, account.id


def _edges(repository: OrganizationRepository) -> dict[str, tuple[str | None, str]]:
    rows = repository.all_edges()
    edges = {row["account_id"]: (row["superior_id"], row["kind"]) for row in rows}
    try:
        validate_structure(edges)
    except (InvalidStructure, ValueError) as exc:
        raise CorruptStructure("stored organization hierarchy is invalid") from exc
    return edges


def get_tree(
    repository: OrganizationRepository,
    *,
    dependencies: OrganizationDependencies,
) -> OrgTreeDto:
    edges = _edges(repository)
    accounts: dict[str, AccountRef] = {}
    for account_id in edges:
        account = dependencies.identity.get(account_id)
        if not _is_effective(account):
            raise CorruptStructure("organization participant is not effective")
        accounts[account_id] = _account_ref(account)

    managers: list[ManagerNode] = []
    manager_ids = [
        account_id
        for account_id, (_superior_id, kind) in edges.items()
        if kind == OrgKind.MANAGER.value
    ]
    for manager_id in sorted(manager_ids, key=lambda value: _sort_key(accounts[value])):
        leader_ids = [
            account_id
            for account_id, (superior_id, kind) in edges.items()
            if superior_id == manager_id and kind == OrgKind.LEADER.value
        ]
        leaders: list[LeaderNode] = []
        for leader_id in sorted(leader_ids, key=lambda value: _sort_key(accounts[value])):
            member_ids = [
                account_id
                for account_id, (superior_id, kind) in edges.items()
                if superior_id == leader_id and kind == OrgKind.MEMBER.value
            ]
            members = [
                accounts[member_id]
                for member_id in sorted(member_ids, key=lambda value: _sort_key(accounts[value]))
            ]
            leaders.append(LeaderNode(account=accounts[leader_id], members=members))
        managers.append(ManagerNode(account=accounts[manager_id], leaders=leaders))
    return OrgTreeDto(managers=managers)


def direct_reports(
    repository: OrganizationRepository,
    *,
    leader_id: str,
    dependencies: OrganizationDependencies,
) -> list[AccountRef]:
    edges = _edges(repository)
    leader_edge = edges.get(leader_id)
    leader = dependencies.identity.get(leader_id)
    if leader_edge is None or leader_edge[1] != OrgKind.LEADER.value or not _is_effective(leader):
        raise CorruptStructure("direct-report owner is not an effective leader")

    reports: list[AccountRef] = []
    for account_id, (superior_id, kind) in edges.items():
        if superior_id != leader_id or kind != OrgKind.MEMBER.value:
            continue
        account = dependencies.identity.get(account_id)
        if _is_effective(account):
            reports.append(_account_ref(account))
    return sorted(reports, key=_sort_key)
