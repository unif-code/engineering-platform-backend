from types import SimpleNamespace

import pytest

import control_plane.app.modules.organization as organization
from control_plane.app.modules.organization.ports import OrganizationAccountView


class RelevantRepository:
    def __init__(self, edges: list[dict[str, object]]) -> None:
        self.edges = edges

    def reporting_edges(self, account_id: str) -> list[dict[str, object]]:
        result = []
        for _ in range(3):
            rows = [row for row in self.edges if row["account_id"] == account_id]
            result.extend(rows)
            if len(rows) != 1 or rows[0]["superior_id"] is None:
                break
            account_id = str(rows[0]["superior_id"])
        return result


@pytest.mark.parametrize("owner", ["member", "leader"])
def test_relevant_reporting_context_routes_member_and_leader_without_unrelated_accounts(
    owner: str,
) -> None:
    rows: list[dict[str, object]] = [
        {"account_id": "member", "superior_id": "leader", "kind": "MEMBER"},
        {"account_id": "leader", "superior_id": "manager", "kind": "LEADER"},
        {"account_id": "manager", "superior_id": None, "kind": "MANAGER"},
        {"account_id": "unrelated-disabled", "superior_id": None, "kind": "MANAGER"},
    ]

    def account(account_id: str) -> OrganizationAccountView:
        assert account_id != "unrelated-disabled"
        return OrganizationAccountView(
            id=account_id,
            employee_no="00000001",
            display_name=account_id,
            status="ENABLED",
            initialized=True,
        )

    dependencies = SimpleNamespace(
        repository_factory=lambda db: RelevantRepository(rows),
        identity=SimpleNamespace(get=account),
    )
    query = getattr(organization, "reporting_context", None)
    assert query is not None, "Organization must expose the narrow reporting context"
    result = query(None, account_id=owner, dependencies=dependencies)
    assert result.reviewer_id == "leader"
    assert result.account_id == owner
    assert len(result.participants) == (3 if owner == "member" else 2)
    assert result.facts_hash.startswith("sha256:")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {"account_id": "member", "superior_id": None, "kind": "MEMBER"},
        ],
        [
            {"account_id": "member", "superior_id": None, "kind": "MANAGER"},
        ],
    ],
)
def test_missing_or_unsupported_reporting_context_denies(rows: list[dict[str, object]]) -> None:
    dependencies = SimpleNamespace(
        repository_factory=lambda db: RelevantRepository(rows),
        identity=SimpleNamespace(get=lambda _: None),
    )
    query = getattr(organization, "reporting_context", None)
    assert query is not None, "Organization must expose the narrow reporting context"
    with pytest.raises(organization.CorruptStructure):
        query(None, account_id="member", dependencies=dependencies)
