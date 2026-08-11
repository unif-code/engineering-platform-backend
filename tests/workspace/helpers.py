from collections.abc import Sequence

from sqlalchemy import Engine

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.workspace import WorkspaceDependencies
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.organization.helpers import (
    FixedClock,
    RandomValues,
    StaticSecrets,
    organization_dependencies,
)


def workspace_dependencies(
    identity_engine: Engine,
    organization_engine: Engine,
) -> WorkspaceDependencies:
    from control_plane.app.modules.workspace.adapters import (
        SqlAlchemyIdentityAccountLookup,
        SqlAlchemyOrganizationReports,
        SqlAlchemyWorkspaceRepository,
    )

    organization_deps = organization_dependencies(
        identity_engine,
        on_membership_change=lambda _ids: None,
    )
    return WorkspaceDependencies(
        repository_factory=SqlAlchemyWorkspaceRepository,
        identity=SqlAlchemyIdentityAccountLookup(identity_engine, identity_dependencies()),
        organization=SqlAlchemyOrganizationReports(organization_engine, organization_deps),
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=FixedClock(),
        random=RandomValues(),
        secret_manager=StaticSecrets(),
    )


def configure_org_leader(
    *,
    owner_engine: Engine,
    organization_engine: Engine,
    identity_engine: Engine,
    manager_id: str,
    leader_id: str,
    member_ids: Sequence[str] = (),
) -> None:
    import control_plane.app.modules.organization as organization
    from control_plane.app.modules.identity import Principal
    from tests.organization.helpers import insert_account

    accounts = [(manager_id, "00000801", "Manager"), (leader_id, "00000802", "Leader")]
    accounts.extend(
        (member_id, f"{803 + index:08d}", f"Member {index}")
        for index, member_id in enumerate(member_ids)
    )
    for account_id, employee_no, display_name in accounts:
        insert_account(
            owner_engine,
            account_id=account_id,
            employee_no=employee_no,
            display_name=display_name,
        )
    deps = organization_dependencies(identity_engine, on_membership_change=lambda _ids: None)
    actor = Principal(employee_id="SYSTEM", name="System")
    with organization_engine.begin() as db:
        organization.set_superior(
            db,
            account_id=manager_id,
            superior_id=None,
            actor=actor,
            reason="workspace test manager",
            dependencies=deps,
        )
        organization.set_superior(
            db,
            account_id=leader_id,
            superior_id=manager_id,
            actor=actor,
            reason="workspace test leader",
            dependencies=deps,
        )
        for member_id in member_ids:
            organization.set_superior(
                db,
                account_id=member_id,
                superior_id=leader_id,
                actor=actor,
                reason="workspace test member",
                dependencies=deps,
            )
