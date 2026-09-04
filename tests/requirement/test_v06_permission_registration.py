import json

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from control_plane.app.bootstrap.app import _DEFAULT_NAVIGATION_ACTION_CAPABILITIES
from control_plane.app.modules.authorization import V02_SUPER_ADMIN_PLATFORM_CAPABILITIES
from tests.requirement.conftest import IsolatedRequirementDatabase

V05 = [
    "work_item.create",
    "work_item.assign",
    "requirement.baseline.submit",
    "requirement.baseline.assign",
    "requirement.baseline.decide",
    "work_item.execute",
    "merge_request.merge",
]
ADDED = [
    "work_item.validation.submit",
    "requirement.evidence.request",
    "requirement.evidence.select",
    "requirement.acceptance.submit",
    "requirement.acceptance.decide",
    "formal_merge_request.request",
    "merge_request.review",
    "requirement.delivery_gate.assign",
]


def test_v06_migration_registers_explicit_workspace_actions_and_reverses_without_grants(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    database = isolated_requirement_database
    config = Config("alembic.ini")
    query = text(
        "SELECT meta->'actionCapabilities' FROM \"authorization\".route_registry "
        "WHERE route_key='requirements'"
    )
    with database.owner.connect() as db:
        registered = db.execute(query).scalar_one()
        grants_before = db.execute(
            text('SELECT count(*) FROM "authorization"."grant"')
        ).scalar_one()
    expected = [{"capability": capability, "scopeType": "WORKSPACE"} for capability in V05 + ADDED]
    assert registered == expected
    assert set(V05 + ADDED) <= _DEFAULT_NAVIGATION_ACTION_CAPABILITIES
    assert set(ADDED).isdisjoint(V02_SUPER_ADMIN_PLATFORM_CAPABILITIES)
    command.downgrade(config, "0008_auth_v05_routes")
    with database.owner.connect() as db:
        assert db.execute(query).scalar_one() == expected[:7]
    command.upgrade(config, "heads")
    with database.owner.connect() as db:
        assert db.execute(query).scalar_one() == expected
        assert (
            db.execute(text('SELECT count(*) FROM "authorization"."grant"')).scalar_one()
            == grants_before
        )
    # An unmanaged action must not be silently overwritten on downgrade.
    with database.owner.begin() as db:
        db.execute(
            text(
                'UPDATE "authorization".route_registry SET meta=jsonb_set(meta, '
                "'{actionCapabilities}', CAST(:value AS jsonb)) WHERE route_key='requirements'"
            ),
            {
                "value": json.dumps(
                    expected + [{"capability": "unmanaged.action", "scopeType": "WORKSPACE"}]
                )
            },
        )
    with pytest.raises(Exception, match="conflicting V0.6"):
        command.downgrade(config, "0008_auth_v05_routes")
