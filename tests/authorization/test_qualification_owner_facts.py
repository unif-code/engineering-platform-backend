import hashlib
import json
from contextlib import ExitStack
from uuid import uuid4

import pytest
from sqlalchemy import text

from control_plane.app.modules.authorization import (
    ActorQualificationRuntime,
    CurrentActorFactsAdapter,
)
from control_plane.app.modules.organization import reporting_context
from tests.authorization.helpers import authorization_dependencies
from tests.configuration.conftest import _temporary_runtime_role_engine
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.organization.helpers import insert_account, organization_dependencies
from tests.requirement.conftest import (  # noqa: F401
    IsolatedRequirementDatabase,
    isolated_requirement_database,
    requirement_owner_engine,
)
from tests.workspace.helpers import configure_org_leader, workspace_dependencies


@pytest.mark.integration
def test_qualification_and_reporting_use_real_restricted_owner_facts(
    request: pytest.FixtureRequest,
) -> None:
    database: IsolatedRequirementDatabase = request.getfixturevalue("isolated_requirement_database")
    actor, manager, member, workspace_id = (str(uuid4()) for _ in range(4))
    auth = authorization_dependencies()
    now = auth.clock.now()
    with ExitStack() as stack:
        engines = {
            role: stack.enter_context(
                _temporary_runtime_role_engine(
                    database.owner, database.owner.url, privilege_role=role
                )
            )[0]
            for role in ("identity_rw", "workspace_rw", "organization_rw", "authorization_rw")
        }
        configure_org_leader(
            owner_engine=database.owner,
            organization_engine=engines["organization_rw"],
            identity_engine=engines["identity_rw"],
            manager_id=manager,
            leader_id=actor,
            member_ids=(member,),
        )
        insert_account(
            database.owner,
            account_id=str(uuid4()),
            employee_no="00000999",
            display_name="Unrelated disabled",
            status="DISABLED",
        )
        with database.owner.begin() as db:
            db.execute(text("UPDATE identity.account SET version=5 WHERE id=:id"), {"id": actor})
            db.execute(
                text(
                    "INSERT INTO workspace.workspace (id,name,owner_id,version) "
                    "VALUES (:w,'Qualification workspace',:a,12)"
                ),
                {"w": workspace_id, "a": actor},
            )
            db.execute(
                text(
                    "INSERT INTO workspace.members_projection "
                    "(workspace_id,account_id,source,computed_at) VALUES (:w,:a,'OWNER',:now)"
                ),
                {"w": workspace_id, "a": actor, "now": now},
            )
            db.execute(
                text(
                    'INSERT INTO "authorization".principal_version '
                    "(account_id,version,fence_generation,updated_at) VALUES (:a,4,7,:now)"
                ),
                {"a": actor, "now": now},
            )
            for capability in ("merge_request.review", "code.change"):
                db.execute(
                    text(
                        'INSERT INTO "authorization"."grant" '
                        "(id,principal_id,capability,scope_type,scope_id,source,version,"
                        "created_at,updated_at) "
                        "VALUES (:id,:a,:cap,'WORKSPACE',:w,'MANUAL',3,:now,:now)"
                    ),
                    {
                        "id": str(uuid4()),
                        "a": actor,
                        "cap": capability,
                        "w": workspace_id,
                        "now": now,
                    },
                )
        runtime = ActorQualificationRuntime(
            engines["authorization_rw"],
            auth,
            CurrentActorFactsAdapter(
                engines["identity_rw"],
                identity_dependencies(),
                engines["workspace_rw"],
                workspace_dependencies(engines["identity_rw"], engines["organization_rw"]),
            ),
        )
        facts = runtime.evaluate(actor, workspace_id, ("merge_request.review", "code.change"))
        assert facts.eligible
        assert (
            facts.account is not None and facts.account.version == 5 and facts.account.initialized
        )
        assert facts.workspace is not None and facts.workspace.version == 12
        assert (
            facts.workspace.member_source == "OWNER" and facts.workspace.member_computed_at == now
        )
        assert facts.principal is not None and facts.principal.version == 4
        assert facts.principal.fence_generation == 7 and facts.principal.dirty_generation is None
        assert len(facts.grants) == 2 and {grant.version for grant in facts.grants} == {3}
        payload = facts.model_dump(mode="json", exclude={"snapshot_hash"})
        assert (
            facts.snapshot_hash
            == "sha256:"
            + hashlib.sha256(
                json.dumps(
                    payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
        )
        org = organization_dependencies(engines["identity_rw"], on_membership_change=lambda _: None)
        with engines["organization_rw"].connect() as db:
            path = reporting_context(db, account_id=member, dependencies=org)
        assert path.reviewer_id == actor and len(path.participants) == 3
        with database.owner.begin() as db:
            db.execute(
                text(
                    'UPDATE "authorization"."grant" SET status=\'REVOKED\',version=4, '
                    "revoked_at=:now,revoked_by='test',revoke_reason='test' "
                    "WHERE capability='code.change'"
                ),
                {"now": now},
            )
            db.execute(
                text('UPDATE "authorization".principal_version SET version=5 WHERE account_id=:a'),
                {"a": actor},
            )
        denied = runtime.evaluate(actor, workspace_id, ("merge_request.review", "code.change"))
        assert not denied.eligible and len(denied.grants) == 1
        with database.owner.begin() as db:
            db.execute(
                text(
                    'UPDATE "authorization".principal_version '
                    "SET dirty_generation=7,dirty_reason='membership' WHERE account_id=:a"
                ),
                {"a": actor},
            )
        assert not runtime.evaluate(actor, workspace_id, ("merge_request.review",)).eligible
