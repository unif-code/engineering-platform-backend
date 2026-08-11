import pytest
from sqlalchemy import Engine, text

import control_plane.app.modules.organization as organization
from control_plane.app.modules.identity import Principal
from control_plane.app.modules.organization import OrganizationDependencies
from tests.organization.helpers import insert_account, organization_dependencies

pytestmark = pytest.mark.integration

MANAGER_ID = "00000000-0000-0000-0000-000000000301"
SECOND_MANAGER_ID = "00000000-0000-0000-0000-000000000302"
LEADER_ID = "00000000-0000-0000-0000-000000000303"
SECOND_LEADER_ID = "00000000-0000-0000-0000-000000000304"
MEMBER_ID = "00000000-0000-0000-0000-000000000305"
SECOND_MEMBER_ID = "00000000-0000-0000-0000-000000000306"
ACTOR = Principal(employee_id="00000999", name="Administrator")


def _account(
    owner_engine: Engine,
    account_id: str,
    employee_no: str,
    display_name: str,
) -> None:
    insert_account(
        owner_engine,
        account_id=account_id,
        employee_no=employee_no,
        display_name=display_name,
    )


def _build_tree(
    owner_engine: Engine,
    rw_engine: Engine,
    identity_engine: Engine,
) -> OrganizationDependencies:
    accounts = [
        (MANAGER_ID, "00000310", "Manager Z"),
        (SECOND_MANAGER_ID, "00000301", "Manager A"),
        (LEADER_ID, "00000320", "Leader Z"),
        (SECOND_LEADER_ID, "00000311", "Leader A"),
        (MEMBER_ID, "00000330", "Member Z"),
        (SECOND_MEMBER_ID, "00000321", "Member A"),
    ]
    for account_id, employee_no, display_name in accounts:
        _account(owner_engine, account_id, employee_no, display_name)
    dependencies = organization_dependencies(
        identity_engine,
        on_membership_change=lambda _ids: None,
    )
    assignments = [
        (MANAGER_ID, None),
        (SECOND_MANAGER_ID, None),
        (LEADER_ID, MANAGER_ID),
        (SECOND_LEADER_ID, MANAGER_ID),
        (MEMBER_ID, LEADER_ID),
        (SECOND_MEMBER_ID, LEADER_ID),
    ]
    with rw_engine.begin() as db:
        for account_id, superior_id in assignments:
            organization.set_superior(
                db,
                account_id=account_id,
                superior_id=superior_id,
                actor=ACTOR,
                reason="build query fixture",
                dependencies=dependencies,
            )
    return dependencies


def test_get_tree_returns_safe_deterministic_three_level_shape(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    dependencies = _build_tree(
        organization_owner_engine,
        organization_rw_engine,
        organization_identity_engine,
    )

    with organization_rw_engine.connect() as db:
        tree = organization.get_tree(db, dependencies=dependencies)

    assert tree.model_dump() == {
        "managers": [
            {
                "account": {
                    "id": SECOND_MANAGER_ID,
                    "employee_no": "00000301",
                    "display_name": "Manager A",
                },
                "leaders": [],
            },
            {
                "account": {
                    "id": MANAGER_ID,
                    "employee_no": "00000310",
                    "display_name": "Manager Z",
                },
                "leaders": [
                    {
                        "account": {
                            "id": SECOND_LEADER_ID,
                            "employee_no": "00000311",
                            "display_name": "Leader A",
                        },
                        "members": [],
                    },
                    {
                        "account": {
                            "id": LEADER_ID,
                            "employee_no": "00000320",
                            "display_name": "Leader Z",
                        },
                        "members": [
                            {
                                "id": SECOND_MEMBER_ID,
                                "employee_no": "00000321",
                                "display_name": "Member A",
                            },
                            {
                                "id": MEMBER_ID,
                                "employee_no": "00000330",
                                "display_name": "Member Z",
                            },
                        ],
                    },
                ],
            },
        ]
    }


def test_direct_reports_returns_enabled_direct_members_only_in_stable_order(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    dependencies = _build_tree(
        organization_owner_engine,
        organization_rw_engine,
        organization_identity_engine,
    )
    with organization_owner_engine.begin() as db:
        db.execute(
            text("UPDATE identity.account SET status='DISABLED' WHERE id=:id"),
            {"id": MEMBER_ID},
        )

    with organization_rw_engine.connect() as db:
        reports = organization.direct_reports(
            db,
            leader_id=LEADER_ID,
            dependencies=dependencies,
        )

    assert [report.model_dump() for report in reports] == [
        {
            "id": SECOND_MEMBER_ID,
            "employee_no": "00000321",
            "display_name": "Member A",
        }
    ]


def test_get_tree_fails_closed_for_database_valid_but_corrupt_structure(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    _account(organization_owner_engine, MANAGER_ID, "00000301", "Manager")
    _account(organization_owner_engine, MEMBER_ID, "00000302", "Member")
    with organization_owner_engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO organization.org_edge (account_id, superior_id, kind) "
                "VALUES (:manager_id, NULL, 'MANAGER'), "
                "(:member_id, :manager_id, 'MEMBER')"
            ),
            {"manager_id": MANAGER_ID, "member_id": MEMBER_ID},
        )
    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=lambda _ids: None,
    )

    corrupt_structure = getattr(organization, "CorruptStructure", ValueError)
    with organization_rw_engine.connect() as db, pytest.raises(corrupt_structure):
        organization.get_tree(db, dependencies=dependencies)
