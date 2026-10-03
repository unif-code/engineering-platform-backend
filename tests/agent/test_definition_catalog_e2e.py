import importlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
from tests.agent.test_business_context_status_e2e import _status_client
from tests.agent.test_repository import DEFINITION
from tests.source_control.test_v06_production_e2e import Journey, _grant, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database

PATH = "/api/v1/agent-definitions"
CAPABILITY = "agent.definition.read"
ROUTE = "agent-definitions"


def catalog_facts(journey: Journey) -> Any:
    with journey.database.owner.connect() as db:
        return [
            db.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY 1")).scalars().all()
            for table in (
                "agent.agent_definition",
                "agent.agent_run",
                "agent.agent_attempt",
                "agent.execution_binding",
                "agent.idempotency_key",
                "audit.audit_event",
                '"authorization"."grant"',
                '"authorization".route_registry',
            )
        ]


def route_keys(client: Any) -> set[str]:
    response = client.get("/api/v1/navigation")
    assert response.status_code == 200, response.text
    return {item["routeKey"] for item in response.json()}


@pytest.mark.integration
def test_default_session_platform_authority_and_navigation_precede_all_catalog_reads(
    journey: Journey,
) -> None:
    runtime = bootstrap.agent_http_runtime()
    reader = Mock(wraps=runtime.dependencies.transaction_runner)
    availability = Mock(wraps=runtime.dependencies.definition_availability.is_active)
    guarded = replace(
        runtime,
        dependencies=replace(
            runtime.dependencies,
            transaction_runner=reader,
            definition_availability=SimpleNamespace(is_active=availability),
        ),
    )
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text('SELECT count(*) FROM "authorization"."grant" WHERE capability=:capability'),
                {"capability": CAPABILITY},
            ).scalar_one()
            == 0
        )
    before = catalog_facts(journey)
    assert ROUTE in route_keys(journey.admin)
    assert journey.admin.get(PATH).status_code == 200
    assert catalog_facts(journey) == before
    with _status_client(journey, guarded) as client:
        client.cookies.clear()
        assert client.get(PATH).status_code == 401
        client.cookies.update(journey.member.cookies)
        assert client.get(PATH).status_code == 403
        assert ROUTE not in route_keys(client)
        for capability in ("agent.run.read", "agent.run.execute", CAPABILITY):
            _grant(journey.admin, journey.member_id, capability, journey.workspace_id)
        assert client.get(PATH).status_code == 403
        assert ROUTE not in route_keys(client)
        reader.assert_not_called()
        availability.assert_not_called()
        _grant(journey.admin, journey.member_id, CAPABILITY)
        before = catalog_facts(journey)
        response = client.get(PATH)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert "etag" not in response.headers
        assert ROUTE in route_keys(client)
        reader.assert_called_once()
        assert availability.called
        assert catalog_facts(journey) == before
        grants = journey.admin.get("/api/v1/admin/grants").json()["items"]
        grant = next(
            item
            for item in grants
            if item["principalId"] == journey.member_id
            and item["capability"] == CAPABILITY
            and item["scopeType"] == "PLATFORM"
            and item["status"] == "ACTIVE"
        )
        _write(
            journey.admin,
            f"/api/v1/admin/grants/{grant['id']}",
            {"reason": "Catalog platform read revoked"},
            method="DELETE",
            etag=f'"v{grant["version"]}"',
        )
        reader.reset_mock()
        availability.reset_mock()
        assert client.get(PATH).status_code == 403
        assert ROUTE not in route_keys(client)
        reader.assert_not_called()
        availability.assert_not_called()


@pytest.mark.integration
def test_real_catalog_keeps_versions_and_exposes_only_boolean_filter_results(
    journey: Journey,
) -> None:
    with journey.database.engines["agent"].begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        repository.insert_definition(DEFINITION.model_copy(update={"version": 2}))
    _grant(journey.admin, journey.member_id, CAPABILITY)
    runtime = bootstrap.agent_http_runtime()
    availability = Mock(return_value=True)
    guarded = replace(
        runtime,
        dependencies=replace(
            runtime.dependencies, definition_availability=SimpleNamespace(is_active=availability)
        ),
    )
    before = catalog_facts(journey)
    with _status_client(journey, guarded) as client:
        response = client.get(PATH)
        assert response.status_code == 200, response.text
        assert [
            row["version"] for row in response.json()["items"] if row["id"] == DEFINITION.id
        ] == [1, 2]
        availability.side_effect = lambda definition: (
            not (definition.id == DEFINITION.id and definition.version == 2)
        )
        filtered = client.get(PATH)
        assert filtered.status_code == 200
        assert [
            row["version"] for row in filtered.json()["items"] if row["id"] == DEFINITION.id
        ] == [1]
        availability.side_effect = None
        availability.return_value = False
        assert client.get(PATH).json() == {"items": []}
        for unknown in (None, 0, "available"):
            availability.return_value = unknown
            invalid = client.get(PATH)
            assert invalid.status_code == 503 and "items" not in invalid.json()
        availability.side_effect = RuntimeError("private-catalog-sentinel")
        unavailable = client.get(PATH)
        assert unavailable.status_code == 503 and "private-catalog" not in unavailable.text
    assert catalog_facts(journey) == before


@pytest.mark.integration
def test_bad_stored_definition_is_503_even_when_availability_would_filter_it(
    journey: Journey,
) -> None:
    # Persist a database-representable predecessor that violates the public name bound.
    with journey.database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO agent.agent_definition (id,version,name,capability_declarations,"
                "skill_declarations,runtime_permissions,input_schema) "
                "VALUES (:id,1,:name,'[]'::jsonb,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb)"
            ),
            {"id": str(uuid4()), "name": "x" * 201},
        )
    _grant(journey.admin, journey.member_id, CAPABILITY)
    runtime = bootstrap.agent_http_runtime()
    availability = Mock(return_value=False)
    guarded = replace(
        runtime,
        dependencies=replace(
            runtime.dependencies, definition_availability=SimpleNamespace(is_active=availability)
        ),
    )
    before = catalog_facts(journey)
    with _status_client(journey, guarded) as client:
        response = client.get(PATH)
        assert response.status_code == 503 and "items" not in response.json()
    availability.assert_not_called()
    assert catalog_facts(journey) == before


@pytest.mark.integration
def test_definition_route_migration_is_idempotent_and_preserves_operator_conflicts(
    journey: Journey,
) -> None:
    migration = importlib.import_module(
        "migrations.authorization.0013_authorization_agent_definitions"
    )
    config = Config("alembic.ini")
    with journey.database.owner.connect() as db:
        before_grants = (
            db.execute(text('SELECT to_jsonb(t) FROM "authorization"."grant" t ORDER BY 1'))
            .scalars()
            .all()
        )
    command.downgrade(config, "0012_auth_agent_control")
    try:
        with journey.database.owner.connect() as db:
            assert (
                db.execute(
                    text(
                        'SELECT count(*) FROM "authorization".route_registry WHERE route_key=:key'
                    ),
                    {"key": ROUTE},
                ).scalar_one()
                == 0
            )
        command.upgrade(config, "heads")
        with journey.database.owner.begin() as db:
            with patch.object(migration, "op", Operations(MigrationContext.configure(db))):
                migration.upgrade()
            row = db.execute(
                text(
                    'SELECT capability,scope_type,sort,meta FROM "authorization".route_registry '
                    "WHERE route_key=:key"
                ),
                {"key": ROUTE},
            ).one()
            assert tuple(row) == (
                CAPABILITY,
                "PLATFORM",
                22,
                {"name": "Agent 定义声明", "order": 22},
            )
        for column, value in (
            ("meta", json.dumps({"name": "Operator protected"})),
            ("scope_type", "WORKSPACE"),
            ("capability", "operator.protected.read"),
        ):
            assignment = "CAST(:value AS JSONB)" if column == "meta" else ":value"
            with journey.database.owner.begin() as db:
                db.execute(
                    text(
                        f'UPDATE "authorization".route_registry SET {column}={assignment} '
                        "WHERE route_key=:key"
                    ),
                    {"value": value, "key": ROUTE},
                )
            with (
                pytest.raises(DBAPIError, match="conflicting managed route"),
                journey.database.owner.begin() as db,
            ):
                with patch.object(migration, "op", Operations(MigrationContext.configure(db))):
                    migration.upgrade()
            with journey.database.owner.begin() as db:
                stored = db.execute(
                    text(
                        f'SELECT {column} FROM "authorization".route_registry WHERE route_key=:key'
                    ),
                    {"key": ROUTE},
                ).scalar_one()
                assert stored == (json.loads(value) if column == "meta" else value)
                with patch.object(migration, "op", Operations(MigrationContext.configure(db))):
                    migration.downgrade()
                assert (
                    db.execute(
                        text(
                            'SELECT count(*) FROM "authorization".route_registry '
                            "WHERE route_key=:key"
                        ),
                        {"key": ROUTE},
                    ).scalar_one()
                    == 1
                )
                db.execute(
                    text(
                        'UPDATE "authorization".route_registry SET capability=:capability,'
                        "scope_type='PLATFORM',sort=22,meta=CAST(:meta AS JSONB) "
                        "WHERE route_key=:key"
                    ),
                    {
                        "capability": CAPABILITY,
                        "meta": json.dumps({"name": "Agent 定义声明", "order": 22}),
                        "key": ROUTE,
                    },
                )
        with journey.database.owner.connect() as db:
            assert (
                db.execute(text('SELECT to_jsonb(t) FROM "authorization"."grant" t ORDER BY 1'))
                .scalars()
                .all()
                == before_grants
            )
    finally:
        command.upgrade(config, "heads")
