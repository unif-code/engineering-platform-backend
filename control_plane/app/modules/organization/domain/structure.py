from collections.abc import Mapping
from enum import StrEnum


class InvalidStructure(ValueError):
    """The organization graph violates the fixed three-level hierarchy."""


class OrgKind(StrEnum):
    MANAGER = "MANAGER"
    LEADER = "LEADER"
    MEMBER = "MEMBER"


def derive_kind(superior_kind: str | OrgKind | None) -> OrgKind:
    if superior_kind is None:
        return OrgKind.MANAGER
    parent = OrgKind(superior_kind)
    if parent is OrgKind.MANAGER:
        return OrgKind.LEADER
    if parent is OrgKind.LEADER:
        return OrgKind.MEMBER
    raise InvalidStructure("ordinary members cannot have direct reports")


def validate_structure(
    edges: Mapping[str, tuple[str | None, str | OrgKind]],
) -> None:
    normalized = {
        account_id: (superior_id, OrgKind(kind))
        for account_id, (superior_id, kind) in edges.items()
    }
    for account_id, (superior_id, kind) in normalized.items():
        if superior_id == account_id:
            raise InvalidStructure("an account cannot report to itself")
        if kind is OrgKind.MANAGER:
            if superior_id is not None:
                raise InvalidStructure("managers cannot have a superior")
            continue
        if superior_id is None or superior_id not in normalized:
            raise InvalidStructure("leader and member superiors must exist")
        parent_kind = normalized[superior_id][1]
        if derive_kind(parent_kind) is not kind:
            raise InvalidStructure("organization levels must remain manager-leader-member")

    for account_id in normalized:
        seen: set[str] = set()
        current: str | None = account_id
        while current is not None:
            if current in seen:
                raise InvalidStructure("organization graph cannot contain cycles")
            seen.add(current)
            current = normalized[current][0]
