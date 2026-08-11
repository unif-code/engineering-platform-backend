from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Event

import pytest
from sqlalchemy import Engine, text

import control_plane.app.modules.organization as organization
from control_plane.app.modules.identity import Principal
from tests.organization.helpers import insert_account, organization_dependencies

pytestmark = pytest.mark.integration

MANAGER_ID = "00000000-0000-0000-0000-000000000201"
LEADER_ID = "00000000-0000-0000-0000-000000000202"
MEMBER_ID = "00000000-0000-0000-0000-000000000203"
OTHER_ID = "00000000-0000-0000-0000-000000000204"
ACTOR = Principal(employee_id="00000999", name="Administrator")


def _set_superior() -> Callable[..., object]:
    return getattr(organization, "set_superior", lambda *_args, **_kwargs: None)


def _accounts(owner_engine: Engine) -> None:
    insert_account(
        owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000201",
        display_name="Manager",
    )
    insert_account(
        owner_engine,
        account_id=LEADER_ID,
        employee_no="00000202",
        display_name="Leader",
    )
    insert_account(
        owner_engine,
        account_id=MEMBER_ID,
        employee_no="00000203",
        display_name="Member",
    )
    insert_account(
        owner_engine,
        account_id=OTHER_ID,
        employee_no="00000204",
        display_name="Other",
    )


def test_set_superior_builds_fixed_levels_and_emits_audit_and_callback_once(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    _accounts(organization_owner_engine)
    callbacks: list[tuple[str, ...]] = []
    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=lambda ids: callbacks.append(tuple(ids)),
    )
    set_superior = _set_superior()

    with organization_rw_engine.begin() as db:
        set_superior(
            db,
            account_id=MANAGER_ID,
            superior_id=None,
            actor=ACTOR,
            reason="create manager",
            dependencies=dependencies,
        )
        set_superior(
            db,
            account_id=LEADER_ID,
            superior_id=MANAGER_ID,
            actor=ACTOR,
            reason="assign leader",
            dependencies=dependencies,
        )
        set_superior(
            db,
            account_id=MEMBER_ID,
            superior_id=LEADER_ID,
            actor=ACTOR,
            reason="assign member",
            dependencies=dependencies,
        )

    with organization_owner_engine.connect() as db:
        rows = db.execute(
            text(
                "SELECT account_id, superior_id, kind FROM organization.org_edge "
                "ORDER BY account_id"
            )
        ).all()
        audits = db.execute(
            text(
                "SELECT actor, actor_type, target_type, target_id, reason "
                "FROM audit.audit_event "
                "WHERE action='organization.structure.changed' ORDER BY target_id"
            )
        ).all()

    assert [tuple(row) for row in rows] == [
        (MANAGER_ID, None, "MANAGER"),
        (LEADER_ID, MANAGER_ID, "LEADER"),
        (MEMBER_ID, LEADER_ID, "MEMBER"),
    ]
    assert callbacks == [(MANAGER_ID,), (LEADER_ID,), (MEMBER_ID,)]
    assert [row.target_id for row in audits] == [MANAGER_ID, LEADER_ID, MEMBER_ID]
    assert {(row.actor, row.actor_type, row.target_type) for row in audits} == {
        (ACTOR.employee_id, "HUMAN", "ACCOUNT")
    }
    assert "old=absent" in audits[0].reason
    assert "new=MANAGER:none" in audits[0].reason

    with organization_rw_engine.begin() as db:
        set_superior(
            db,
            account_id=MEMBER_ID,
            superior_id=LEADER_ID,
            actor=ACTOR,
            reason="same assignment",
            dependencies=dependencies,
        )
    with organization_owner_engine.connect() as db:
        audit_count = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='organization.structure.changed'"
            )
        ).scalar_one()
    assert callbacks == [(MANAGER_ID,), (LEADER_ID,), (MEMBER_ID,)]
    assert audit_count == 3


@pytest.mark.parametrize(
    ("target_status", "target_initialized", "superior_status", "superior_initialized"),
    [
        ("DISABLED", True, "ENABLED", True),
        ("RESTRICTED", True, "ENABLED", True),
        ("PENDING_INIT", False, "ENABLED", True),
        ("ENABLED", False, "ENABLED", True),
        ("ENABLED", True, "DISABLED", True),
        ("ENABLED", True, "ENABLED", False),
    ],
)
def test_set_superior_fails_closed_for_ineffective_participants(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
    target_status: str,
    target_initialized: bool,
    superior_status: str,
    superior_initialized: bool,
) -> None:
    insert_account(
        organization_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000201",
        display_name="Target",
        status=target_status,
        initialized=target_initialized,
    )
    insert_account(
        organization_owner_engine,
        account_id=LEADER_ID,
        employee_no="00000202",
        display_name="Superior",
        status=superior_status,
        initialized=superior_initialized,
    )
    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=lambda _ids: None,
    )
    invalid_participant = getattr(organization, "InvalidParticipant", ValueError)

    with organization_rw_engine.begin() as db, pytest.raises(invalid_participant):
        _set_superior()(
            db,
            account_id=MANAGER_ID,
            superior_id=LEADER_ID,
            actor=ACTOR,
            reason="invalid participant",
            dependencies=dependencies,
        )


def test_reclassification_that_would_leave_invalid_descendants_is_rejected(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    _accounts(organization_owner_engine)
    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=lambda _ids: None,
    )
    set_superior = _set_superior()
    with organization_rw_engine.begin() as db:
        set_superior(
            db,
            account_id=MANAGER_ID,
            superior_id=None,
            actor=ACTOR,
            reason="manager",
            dependencies=dependencies,
        )
        set_superior(
            db,
            account_id=LEADER_ID,
            superior_id=MANAGER_ID,
            actor=ACTOR,
            reason="leader",
            dependencies=dependencies,
        )
        set_superior(
            db,
            account_id=MEMBER_ID,
            superior_id=LEADER_ID,
            actor=ACTOR,
            reason="member",
            dependencies=dependencies,
        )
        set_superior(
            db,
            account_id=OTHER_ID,
            superior_id=None,
            actor=ACTOR,
            reason="other manager",
            dependencies=dependencies,
        )

    invalid_structure = getattr(organization, "InvalidStructure", ValueError)
    with organization_rw_engine.begin() as db, pytest.raises(invalid_structure):
        set_superior(
            db,
            account_id=MANAGER_ID,
            superior_id=OTHER_ID,
            actor=ACTOR,
            reason="invalid reclassification",
            dependencies=dependencies,
        )


def test_callback_failure_rolls_back_organization_fact_and_audit(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    insert_account(
        organization_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000201",
        display_name="Manager",
    )

    def fail_callback(_ids: object) -> None:
        raise RuntimeError("projection unavailable")

    dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=fail_callback,
    )

    with pytest.raises(RuntimeError, match="projection unavailable"):
        with organization_rw_engine.begin() as db:
            _set_superior()(
                db,
                account_id=MANAGER_ID,
                superior_id=None,
                actor=ACTOR,
                reason="must roll back",
                dependencies=dependencies,
            )

    with organization_owner_engine.connect() as db:
        edge_count = db.execute(text("SELECT count(*) FROM organization.org_edge")).scalar_one()
        audit_count = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='organization.structure.changed'"
            )
        ).scalar_one()
    assert edge_count == 0
    assert audit_count == 0


def test_concurrent_cross_assignment_is_serialized_and_cannot_create_a_cycle(
    organization_owner_engine: Engine,
    organization_rw_engine: Engine,
    organization_identity_engine: Engine,
    clean_organization_db: None,
) -> None:
    insert_account(
        organization_owner_engine,
        account_id=MANAGER_ID,
        employee_no="00000201",
        display_name="Manager A",
    )
    insert_account(
        organization_owner_engine,
        account_id=OTHER_ID,
        employee_no="00000204",
        display_name="Manager B",
    )
    setup_dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=lambda _ids: None,
    )
    set_superior = _set_superior()
    with organization_rw_engine.begin() as db:
        set_superior(
            db,
            account_id=MANAGER_ID,
            superior_id=None,
            actor=ACTOR,
            reason="manager A",
            dependencies=setup_dependencies,
        )
        set_superior(
            db,
            account_id=OTHER_ID,
            superior_id=None,
            actor=ACTOR,
            reason="manager B",
            dependencies=setup_dependencies,
        )

    first_written = Event()
    release_first = Event()

    def hold_first_callback(_ids: object) -> None:
        first_written.set()
        assert release_first.wait(timeout=5)

    first_dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=hold_first_callback,
    )
    second_dependencies = organization_dependencies(
        organization_identity_engine,
        on_membership_change=lambda _ids: None,
    )

    def assign_a_under_b() -> None:
        with organization_rw_engine.begin() as db:
            set_superior(
                db,
                account_id=MANAGER_ID,
                superior_id=OTHER_ID,
                actor=ACTOR,
                reason="A under B",
                dependencies=first_dependencies,
            )

    def assign_b_under_a() -> None:
        with organization_rw_engine.begin() as db:
            set_superior(
                db,
                account_id=OTHER_ID,
                superior_id=MANAGER_ID,
                actor=ACTOR,
                reason="B under A",
                dependencies=second_dependencies,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(assign_a_under_b)
        assert first_written.wait(timeout=3)
        second = pool.submit(assign_b_under_a)
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.5)
        finally:
            release_first.set()
        first.result(timeout=3)
        with pytest.raises(organization.InvalidStructure):
            second.result(timeout=3)

    with organization_owner_engine.connect() as db:
        rows = db.execute(
            text(
                "SELECT account_id, superior_id, kind FROM organization.org_edge "
                "ORDER BY account_id"
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        (MANAGER_ID, OTHER_ID, "LEADER"),
        (OTHER_ID, None, "MANAGER"),
    ]
