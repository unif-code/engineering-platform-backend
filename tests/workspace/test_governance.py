from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from importlib import import_module
from threading import Event

import pytest
from sqlalchemy import Engine, text

import control_plane.app.modules.organization as organization
import control_plane.app.modules.workspace as workspace
from control_plane.app.modules.identity import Principal
from control_plane.app.modules.workspace import WorkspaceDependencies
from control_plane.app.modules.workspace.ports import DirectReportView
from tests.organization.helpers import insert_account, organization_dependencies
from tests.workspace.helpers import configure_org_leader, workspace_dependencies

pytestmark = pytest.mark.integration

MANAGER_ID = "00000000-0000-0000-0000-000000000801"
OWNER_ID = "00000000-0000-0000-0000-000000000802"
INVITED_ID = "00000000-0000-0000-0000-000000000803"
UNINVITED_ID = "00000000-0000-0000-0000-000000000804"
OWNER_MEMBER_ID = "00000000-0000-0000-0000-000000000805"
INVITED_MEMBER_ID = "00000000-0000-0000-0000-000000000806"
UNINVITED_MEMBER_ID = "00000000-0000-0000-0000-000000000807"


def test_valid_leader_creates_workspace_with_owner_projection_and_audit(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    configure_org_leader(
        owner_engine=workspace_owner_engine,
        organization_engine=workspace_organization_engine,
        identity_engine=workspace_identity_engine,
        manager_id=MANAGER_ID,
        leader_id=OWNER_ID,
    )
    try:
        workspace = import_module("control_plane.app.modules.workspace")
    except ModuleNotFoundError:
        workspace = None
    create_workspace = getattr(workspace, "create_workspace", lambda *_args, **_kwargs: None)
    dependencies = (
        workspace_dependencies(
            workspace_identity_engine,
            workspace_organization_engine,
        )
        if workspace is not None
        else None
    )

    with workspace_rw_engine.begin() as db:
        created = create_workspace(
            db,
            name="Access Governance",
            owner_id=OWNER_ID,
            actor=Principal(employee_id=OWNER_ID, name="Owner"),
            reason="create workspace",
            dependencies=dependencies,
        )

    with workspace_owner_engine.connect() as db:
        row = db.execute(
            text("SELECT name, owner_id, archived_at, version FROM workspace.workspace")
        ).one_or_none()
        members = db.execute(
            text("SELECT account_id, source FROM workspace.members_projection ORDER BY account_id")
        ).all()
        audit_actions = list(
            db.execute(
                text(
                    "SELECT action FROM audit.audit_event "
                    "WHERE action LIKE 'workspace.%' ORDER BY action"
                )
            ).scalars()
        )

    assert created is not None
    assert row == ("Access Governance", OWNER_ID, None, 1)
    assert [tuple(member) for member in members] == [(OWNER_ID, "OWNER")]
    assert audit_actions == ["workspace.created", "workspace.members.recomputed"]


def _insert_org_accounts(owner_engine: Engine) -> None:
    rows = [
        (MANAGER_ID, "00000801", "Manager"),
        (OWNER_ID, "00000802", "Owner"),
        (INVITED_ID, "00000803", "Invited"),
        (UNINVITED_ID, "00000804", "Uninvited"),
        (OWNER_MEMBER_ID, "00000805", "Owner member"),
        (INVITED_MEMBER_ID, "00000806", "Invited member"),
        (UNINVITED_MEMBER_ID, "00000807", "Uninvited member"),
    ]
    for account_id, employee_no, display_name in rows:
        insert_account(
            owner_engine,
            account_id=account_id,
            employee_no=employee_no,
            display_name=display_name,
        )


def _configure_full_org(identity_engine: Engine, organization_engine: Engine) -> None:
    deps = organization_dependencies(identity_engine, on_membership_change=lambda _ids: None)
    actor = Principal(employee_id="SYSTEM", name="System")
    assignments = [
        (MANAGER_ID, None),
        (OWNER_ID, MANAGER_ID),
        (INVITED_ID, MANAGER_ID),
        (UNINVITED_ID, MANAGER_ID),
        (OWNER_MEMBER_ID, OWNER_ID),
        (INVITED_MEMBER_ID, INVITED_ID),
        (UNINVITED_MEMBER_ID, UNINVITED_ID),
    ]
    with organization_engine.begin() as db:
        for account_id, superior_id in assignments:
            organization.set_superior(
                db,
                account_id=account_id,
                superior_id=superior_id,
                actor=actor,
                reason="workspace full organization fixture",
                dependencies=deps,
            )


def _create(
    workspace_rw_engine: Engine,
    dependencies: WorkspaceDependencies,
    *,
    owner_id: str = OWNER_ID,
    name: str = "Governance",
) -> workspace.WorkspaceDto:
    with workspace_rw_engine.begin() as db:
        return workspace.create_workspace(
            db,
            name=name,
            owner_id=owner_id,
            actor=Principal(employee_id=owner_id, name="Owner"),
            reason="create",
            dependencies=dependencies,
        )


@pytest.mark.parametrize(
    ("status", "initialized", "as_manager"),
    [
        ("DISABLED", True, False),
        ("RESTRICTED", True, False),
        ("PENDING_INIT", False, False),
        ("ENABLED", False, False),
        ("ENABLED", True, True),
    ],
)
def test_create_fails_closed_for_non_effective_or_non_leader_owner(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
    status: str,
    initialized: bool,
    as_manager: bool,
) -> None:
    insert_account(
        workspace_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000801",
        display_name="Manager",
    )
    insert_account(
        workspace_owner_engine,
        account_id=OWNER_ID,
        employee_no="00000802",
        display_name="Candidate",
        status=status,
        initialized=initialized,
    )
    deps = organization_dependencies(
        workspace_identity_engine,
        on_membership_change=lambda _ids: None,
    )
    actor = Principal(employee_id="SYSTEM", name="System")
    with workspace_organization_engine.begin() as db:
        organization.set_superior(
            db,
            account_id=MANAGER_ID,
            superior_id=None,
            actor=actor,
            reason="manager",
            dependencies=deps,
        )
        if status == "ENABLED" and initialized:
            organization.set_superior(
                db,
                account_id=OWNER_ID,
                superior_id=None if as_manager else MANAGER_ID,
                actor=actor,
                reason="candidate",
                dependencies=deps,
            )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )

    with workspace_rw_engine.begin() as db, pytest.raises(workspace.InvalidWorkspaceParticipant):
        workspace.create_workspace(
            db,
            name="Denied",
            owner_id=OWNER_ID,
            actor=Principal(employee_id=OWNER_ID, name="Candidate"),
            reason="invalid owner",
            dependencies=dependencies,
        )

    with workspace_owner_engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM workspace.workspace")).scalar_one() == 0


def test_projection_formula_owner_gate_versioning_and_transfer_cleanup(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    _insert_org_accounts(workspace_owner_engine)
    _configure_full_org(workspace_identity_engine, workspace_organization_engine)
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    owner = Principal(employee_id=OWNER_ID, name="Owner")
    created = _create(workspace_rw_engine, dependencies)

    with workspace_rw_engine.begin() as db:
        invited = workspace.invite_leader(
            db,
            workspace_id=created.id,
            account_id=INVITED_ID,
            expected_version=1,
            actor=owner,
            reason="invite",
            dependencies=dependencies,
        )
    assert invited.version == 2

    with workspace_rw_engine.connect() as db:
        projected = [
            (member.account_id, member.source.value)
            for member in workspace.members(
                db,
                workspace_id=created.id,
                dependencies=dependencies,
            )
        ]
    assert projected == [
        (OWNER_ID, "OWNER"),
        (INVITED_ID, "LEADER"),
        (OWNER_MEMBER_ID, "DIRECT_REPORT"),
        (INVITED_MEMBER_ID, "DIRECT_REPORT"),
    ]
    assert UNINVITED_ID not in {account_id for account_id, _source in projected}
    assert UNINVITED_MEMBER_ID not in {account_id for account_id, _source in projected}

    with workspace_rw_engine.begin() as db, pytest.raises(workspace.StaleWorkspaceVersion):
        workspace.invite_leader(
            db,
            workspace_id=created.id,
            account_id=UNINVITED_ID,
            expected_version=1,
            actor=owner,
            reason="stale",
            dependencies=dependencies,
        )
    with workspace_rw_engine.begin() as db, pytest.raises(workspace.WorkspaceOwnerRequired):
        workspace.invite_leader(
            db,
            workspace_id=created.id,
            account_id=UNINVITED_ID,
            expected_version=2,
            actor=Principal(employee_id=INVITED_ID, name="Invited"),
            reason="not owner",
            dependencies=dependencies,
        )
    with workspace_rw_engine.begin() as db, pytest.raises(workspace.OwnerCannotBeRemoved):
        workspace.remove_leader(
            db,
            workspace_id=created.id,
            account_id=OWNER_ID,
            expected_version=2,
            actor=owner,
            reason="owner cannot leave",
            dependencies=dependencies,
        )

    with workspace_rw_engine.begin() as db:
        transferred = workspace.transfer_owner(
            db,
            workspace_id=created.id,
            new_owner_id=INVITED_ID,
            expected_version=2,
            actor=owner,
            reason="transfer",
            dependencies=dependencies,
        )

    assert transferred.version == 3
    assert transferred.owner_id == INVITED_ID
    with workspace_rw_engine.connect() as db:
        after = [
            (member.account_id, member.source.value)
            for member in workspace.members(
                db,
                workspace_id=created.id,
                dependencies=dependencies,
            )
        ]
    assert after == [
        (INVITED_ID, "OWNER"),
        (INVITED_MEMBER_ID, "DIRECT_REPORT"),
    ]


def test_formal_membership_rechecks_account_state_before_projection_rebuild(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    configure_org_leader(
        owner_engine=workspace_owner_engine,
        organization_engine=workspace_organization_engine,
        identity_engine=workspace_identity_engine,
        manager_id=MANAGER_ID,
        leader_id=OWNER_ID,
        member_ids=[OWNER_MEMBER_ID],
    )
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    created = _create(workspace_rw_engine, dependencies)
    with workspace_owner_engine.begin() as db:
        db.execute(
            text("UPDATE identity.account SET status='DISABLED' WHERE id=:id"),
            {"id": OWNER_MEMBER_ID},
        )

    with workspace_rw_engine.connect() as db:
        assert (
            workspace.is_formal_member(
                db,
                workspace_id=created.id,
                account_id=OWNER_MEMBER_ID,
                dependencies=dependencies,
            )
            is False
        )
        assert OWNER_MEMBER_ID not in {
            member.account_id
            for member in workspace.members(
                db,
                workspace_id=created.id,
                dependencies=dependencies,
            )
        }
    with workspace_rw_engine.begin() as db:
        count = workspace.recompute_members(
            db,
            workspace_id=created.id,
            dependencies=dependencies,
        )
    with workspace_owner_engine.connect() as db:
        projected = list(
            db.execute(
                text(
                    "SELECT account_id FROM workspace.members_projection "
                    "WHERE workspace_id=:workspace_id ORDER BY account_id"
                ),
                {"workspace_id": created.id},
            ).scalars()
        )
        version = db.execute(
            text("SELECT version FROM workspace.workspace WHERE id=:workspace_id"),
            {"workspace_id": created.id},
        ).scalar_one()
    assert count == 1
    assert projected == [OWNER_ID]
    assert version == 2


def test_membership_change_recomputes_only_old_and_new_affected_workspaces(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    _insert_org_accounts(workspace_owner_engine)
    _configure_full_org(workspace_identity_engine, workspace_organization_engine)
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    first = _create(workspace_rw_engine, dependencies, owner_id=OWNER_ID, name="First")
    second = _create(workspace_rw_engine, dependencies, owner_id=INVITED_ID, name="Second")
    third = _create(workspace_rw_engine, dependencies, owner_id=UNINVITED_ID, name="Third")

    org_deps = organization_dependencies(
        workspace_identity_engine,
        on_membership_change=lambda _ids: None,
    )
    with workspace_organization_engine.begin() as db:
        organization.set_superior(
            db,
            account_id=OWNER_MEMBER_ID,
            superior_id=INVITED_ID,
            actor=Principal(employee_id="SYSTEM", name="System"),
            reason="move member",
            dependencies=org_deps,
        )

    handler = workspace.on_membership_change(
        workspace_rw_engine,
        dependencies=dependencies,
    )
    handler([OWNER_MEMBER_ID])

    with workspace_owner_engine.connect() as db:
        versions: dict[str, int] = {
            str(row[0]): int(row[1])
            for row in db.execute(
                text("SELECT name, version FROM workspace.workspace ORDER BY name")
            ).all()
        }
        memberships = set(
            db.execute(
                text(
                    "SELECT w.name, p.account_id FROM workspace.members_projection p "
                    "JOIN workspace.workspace w ON w.id=p.workspace_id "
                    "WHERE p.account_id=:account_id"
                ),
                {"account_id": OWNER_MEMBER_ID},
            ).all()
        )
    assert versions == {"First": 2, "Second": 2, "Third": 1}
    assert memberships == {("Second", OWNER_MEMBER_ID)}
    assert {first.id, second.id, third.id}

    with workspace_rw_engine.connect() as db:
        before_full_rebuild = {
            (str(row[0]), row[1], row[2])
            for row in db.execute(
                text(
                    "SELECT workspace_id, account_id, source "
                    "FROM workspace.members_projection ORDER BY workspace_id, account_id"
                )
            ).all()
        }
    with workspace_rw_engine.begin() as db:
        workspace.recompute_members(
            db,
            workspace_id=first.id,
            dependencies=dependencies,
        )
        workspace.recompute_members(
            db,
            workspace_id=second.id,
            dependencies=dependencies,
        )
    with workspace_rw_engine.connect() as db:
        after_full_rebuild = {
            (str(row[0]), row[1], row[2])
            for row in db.execute(
                text(
                    "SELECT workspace_id, account_id, source "
                    "FROM workspace.members_projection ORDER BY workspace_id, account_id"
                )
            ).all()
        }
        after_versions: dict[str, int] = {
            str(row[0]): int(row[1])
            for row in db.execute(
                text("SELECT name, version FROM workspace.workspace ORDER BY name")
            ).all()
        }
    assert after_full_rebuild == before_full_rebuild
    assert after_versions == versions


def test_membership_change_query_failure_rolls_back_all_workspace_effects(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    _insert_org_accounts(workspace_owner_engine)
    _configure_full_org(workspace_identity_engine, workspace_organization_engine)
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    first_id = "00000000-0000-0000-0000-000000000811"
    second_id = "00000000-0000-0000-0000-000000000812"
    with workspace_owner_engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO workspace.workspace (id, name, owner_id) VALUES "
                "(:first_id, 'First', :first_owner), (:second_id, 'Second', :second_owner)"
            ),
            {
                "first_id": first_id,
                "first_owner": OWNER_ID,
                "second_id": second_id,
                "second_owner": INVITED_ID,
            },
        )
        db.execute(
            text(
                "INSERT INTO workspace.members_projection "
                "(workspace_id, account_id, source, computed_at) VALUES "
                "(:first_id, :first_owner, 'OWNER', now()), "
                "(:first_id, :member, 'DIRECT_REPORT', now()), "
                "(:second_id, :second_owner, 'OWNER', now())"
            ),
            {
                "first_id": first_id,
                "first_owner": OWNER_ID,
                "member": OWNER_MEMBER_ID,
                "second_id": second_id,
                "second_owner": INVITED_ID,
            },
        )
    org_deps = organization_dependencies(
        workspace_identity_engine,
        on_membership_change=lambda _ids: None,
    )
    with workspace_organization_engine.begin() as db:
        organization.set_superior(
            db,
            account_id=OWNER_MEMBER_ID,
            superior_id=INVITED_ID,
            actor=Principal(employee_id="SYSTEM", name="System"),
            reason="move before failed projection",
            dependencies=org_deps,
        )

    real_organization = dependencies.organization

    class FailingOrganization:
        def is_effective_leader(self, account_id: str) -> bool:
            if account_id == INVITED_ID:
                raise RuntimeError("organization query unavailable")
            return real_organization.is_effective_leader(account_id)

        def direct_reports(self, leader_id: str) -> list[DirectReportView]:
            return real_organization.direct_reports(leader_id)

    failing = replace(dependencies, organization=FailingOrganization())
    handler = workspace.on_membership_change(
        workspace_rw_engine,
        dependencies=failing,
    )

    with pytest.raises(RuntimeError, match="organization query unavailable"):
        handler([OWNER_MEMBER_ID])

    with workspace_owner_engine.connect() as db:
        versions: dict[str, int] = {
            str(row[0]): int(row[1])
            for row in db.execute(
                text("SELECT name, version FROM workspace.workspace ORDER BY name")
            ).all()
        }
        memberships = set(
            db.execute(
                text(
                    "SELECT workspace_id::text, account_id "
                    "FROM workspace.members_projection ORDER BY workspace_id, account_id"
                )
            ).all()
        )
        audit_count = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event WHERE action='workspace.members.recomputed'"
            )
        ).scalar_one()
    assert versions == {"First": 1, "Second": 1}
    assert memberships == {
        (first_id, OWNER_ID),
        (first_id, OWNER_MEMBER_ID),
        (second_id, INVITED_ID),
    }
    assert audit_count == 0


def test_concurrent_governance_commands_serialize_on_workspace_version(
    workspace_owner_engine: Engine,
    workspace_rw_engine: Engine,
    workspace_identity_engine: Engine,
    workspace_organization_engine: Engine,
    clean_workspace_db: None,
) -> None:
    _insert_org_accounts(workspace_owner_engine)
    _configure_full_org(workspace_identity_engine, workspace_organization_engine)
    dependencies = workspace_dependencies(
        workspace_identity_engine,
        workspace_organization_engine,
    )
    created = _create(workspace_rw_engine, dependencies)
    entered = Event()
    release = Event()
    real_organization = dependencies.organization

    class BlockingOrganization:
        def is_effective_leader(self, account_id: str) -> bool:
            if account_id == INVITED_ID:
                entered.set()
                assert release.wait(timeout=5)
            return real_organization.is_effective_leader(account_id)

        def direct_reports(self, leader_id: str) -> list[DirectReportView]:
            return real_organization.direct_reports(leader_id)

    blocking = replace(dependencies, organization=BlockingOrganization())
    owner = Principal(employee_id=OWNER_ID, name="Owner")

    def invite(account_id: str, deps: WorkspaceDependencies) -> workspace.WorkspaceDto:
        with workspace_rw_engine.begin() as db:
            return workspace.invite_leader(
                db,
                workspace_id=created.id,
                account_id=account_id,
                expected_version=1,
                actor=owner,
                reason="concurrent invite",
                dependencies=deps,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invite, INVITED_ID, blocking)
        assert entered.wait(timeout=3)
        second = pool.submit(invite, UNINVITED_ID, dependencies)
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.5)
        finally:
            release.set()
        assert first.result(timeout=3).version == 2
        with pytest.raises(workspace.StaleWorkspaceVersion):
            second.result(timeout=3)

    with workspace_owner_engine.connect() as db:
        leaders = list(
            db.execute(
                text("SELECT account_id FROM workspace.leader ORDER BY account_id")
            ).scalars()
        )
    assert leaders == [INVITED_ID]
