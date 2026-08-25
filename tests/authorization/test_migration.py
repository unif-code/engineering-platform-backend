from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from control_plane.app.modules.authorization import V02_SUPER_ADMIN_PLATFORM_CAPABILITIES

pytestmark = pytest.mark.integration


def test_authorization_0006_installs_workspace_requirement_route_without_super_admin_bypass(
    authorization_owner_engine: Engine,
) -> None:
    config = Config("alembic.ini")
    capabilities = {
        "requirement.create",
        "requirement.read",
        "requirement.baseline.submit",
        "requirement.baseline.decide",
        "work_item.assign",
    }
    command.downgrade(config, "0005_authorization_v02_routes")
    try:
        command.upgrade(config, "heads")
        with authorization_owner_engine.connect() as db:
            route = tuple(
                db.execute(
                    text(
                        "SELECT route_key, capability, scope_type, sort, meta "
                        'FROM "authorization".route_registry '
                        "WHERE route_key='requirements'"
                    )
                ).one()
            )
        assert route == (
            "requirements",
            "requirement.read",
            "WORKSPACE",
            20,
            {"name": "Requirements", "order": 20},
        )
        assert capabilities.isdisjoint(V02_SUPER_ADMIN_PLATFORM_CAPABILITIES)

        command.downgrade(config, "0005_authorization_v02_routes")
        with authorization_owner_engine.connect() as db:
            assert db.execute(
                text(
                    "SELECT count(*) FROM \"authorization\".route_registry "
                    "WHERE route_key='requirements'"
                )
            ).scalar_one() == 0
    finally:
        command.upgrade(config, "heads")


def test_authorization_0005_installs_exact_v02_routes_and_preserves_extensions(
    authorization_owner_engine: Engine,
) -> None:
    config = Config("alembic.ini")
    managed_route_keys = (
        "audit",
        "admin.workspaces",
        "admin.organization",
        "admin.users",
        "admin.grants",
        "admin.policies",
    )
    expected_managed_routes = [
        ("audit", "audit.read", "PLATFORM", 7, {"name": "审计看板", "order": 7}),
        (
            "admin.workspaces",
            "platform.workspace.manage",
            "PLATFORM",
            8,
            {"name": "工作区管理", "order": 8},
        ),
        (
            "admin.organization",
            "platform.organization.manage",
            "PLATFORM",
            9,
            {"name": "组织管理", "order": 9},
        ),
        (
            "admin.users",
            "identity.account.manage",
            "PLATFORM",
            13,
            {"name": "用户管理", "order": 13},
        ),
        (
            "admin.grants",
            "platform.authorization.manage",
            "PLATFORM",
            14,
            {"name": "Grant 管理", "order": 14},
        ),
        (
            "admin.policies",
            "platform.configuration.manage",
            "PLATFORM",
            15,
            {"name": "Policy 发布", "order": 15},
        ),
    ]
    extension = (
        "custom.extension",
        "custom.extension.read",
        "PLATFORM",
        99,
        {"name": "Custom Extension", "order": 99, "owner": "extension"},
    )
    command.downgrade(config, "0004_authorization_pending_set")
    try:
        with authorization_owner_engine.begin() as db:
            db.execute(
                text(
                    'INSERT INTO "authorization".route_registry '
                    "(route_key, capability, scope_type, sort, meta) VALUES "
                    "(:route_key, :capability, :scope_type, :sort, CAST(:meta AS JSONB))"
                ),
                {
                    "route_key": extension[0],
                    "capability": extension[1],
                    "scope_type": extension[2],
                    "sort": extension[3],
                    "meta": '{"name":"Custom Extension","order":99,"owner":"extension"}',
                },
            )

        command.upgrade(config, "heads")
        with authorization_owner_engine.connect() as db:
            managed_routes = [
                tuple(row)
                for row in db.execute(
                    text(
                        "SELECT route_key, capability, scope_type, sort, meta "
                        'FROM "authorization".route_registry '
                        "WHERE route_key = ANY(:route_keys) ORDER BY sort, route_key"
                    ),
                    {"route_keys": list(managed_route_keys)},
                )
            ]
            preserved_extension = tuple(
                db.execute(
                    text(
                        "SELECT route_key, capability, scope_type, sort, meta "
                        'FROM "authorization".route_registry WHERE route_key=:route_key'
                    ),
                    {"route_key": extension[0]},
                ).one()
            )
        assert managed_routes == expected_managed_routes
        assert preserved_extension == extension

        with authorization_owner_engine.begin() as db:
            db.execute(
                text(
                    'UPDATE "authorization".route_registry '
                    "SET meta=jsonb_set(meta, '{name}', to_jsonb('环境审计'::text)) "
                    "WHERE route_key='audit'"
                )
            )
        command.downgrade(config, "0004_authorization_pending_set")
        with authorization_owner_engine.connect() as db:
            remaining = [
                tuple(row)
                for row in db.execute(
                    text(
                        "SELECT route_key, capability, scope_type, sort, meta "
                        'FROM "authorization".route_registry '
                        "WHERE route_key = ANY(:route_keys) ORDER BY route_key"
                    ),
                    {"route_keys": [*managed_route_keys, extension[0]]},
                )
            ]
        assert remaining == [
            (
                "audit",
                "audit.read",
                "PLATFORM",
                7,
                {"name": "环境审计", "order": 7},
            ),
            extension,
        ]
    finally:
        command.downgrade(config, "0004_authorization_pending_set")
        with authorization_owner_engine.begin() as db:
            db.execute(
                text(
                    'DELETE FROM "authorization".route_registry WHERE route_key = ANY(:route_keys)'
                ),
                {"route_keys": [*managed_route_keys, extension[0]]},
            )
        command.upgrade(config, "heads")


def test_authorization_0005_rejects_conflicting_managed_route(
    authorization_owner_engine: Engine,
) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0004_authorization_pending_set")
    try:
        with authorization_owner_engine.begin() as db:
            db.execute(
                text(
                    'INSERT INTO "authorization".route_registry '
                    "(route_key, capability, scope_type, sort, meta) VALUES "
                    "('audit', 'wrong.capability', 'PLATFORM', 7, "
                    "jsonb_build_object('name', '审计看板', 'order', 7))"
                )
            )

        with pytest.raises(ProgrammingError, match="conflicting managed route: audit"):
            command.upgrade(config, "heads")
    finally:
        command.downgrade(config, "0004_authorization_pending_set")
        with authorization_owner_engine.begin() as db:
            db.execute(text("DELETE FROM \"authorization\".route_registry WHERE route_key='audit'"))
        command.upgrade(config, "heads")


def test_authorization_0004_roundtrip_backfills_and_preserves_pending_work(
    authorization_owner_engine: Engine,
) -> None:
    config = Config("alembic.ini")
    account_id = "migration-backfill-account"
    work_id = "00000000-0000-0000-0000-000000000919"
    retry_work_id = "00000000-0000-0000-0000-000000000920"
    command.downgrade(config, "0003_authorization_source_xid")
    try:
        with authorization_owner_engine.begin() as db:
            db.execute(
                text(
                    'INSERT INTO "authorization".principal_version '
                    "(account_id, version, fence_generation, dirty_generation, "
                    "dirty_reason, updated_at) VALUES "
                    "(:account_id, 7, 3, 3, 'migration backfill', now())"
                ),
                {"account_id": account_id},
            )
            db.execute(
                text(
                    'INSERT INTO "authorization".convergence_work '
                    "(id, source_module, actor, operation, idempotency_key, reason, "
                    "source_transaction_id, generation_map, affected_account_ids, "
                    "affected_workspace_ids, recompute_membership, status, phase, "
                    "created_at, updated_at) VALUES "
                    "(:id, 'identity', 'actor', 'status', 'migration-backfill-key', "
                    "'migration backfill', NULL, "
                    "jsonb_build_object(CAST(:account_id AS TEXT), 3), "
                    "jsonb_build_array(CAST(:account_id AS TEXT)), '[]'::jsonb, "
                    "true, 'PENDING', "
                    "'SOURCE_REGISTERED', now(), now())"
                ),
                {"id": work_id, "account_id": account_id},
            )

        command.upgrade(config, "heads")
        with authorization_owner_engine.connect() as db:
            first = db.execute(
                text(
                    'SELECT account_id, generation, reason FROM "authorization".'
                    "convergence_principal_pending WHERE work_id=:id"
                ),
                {"id": work_id},
            ).one()
        assert first == (account_id, 3, "migration backfill")

        with authorization_owner_engine.begin() as db:
            db.execute(
                text(
                    'INSERT INTO "authorization".convergence_work '
                    "(id, source_module, actor, operation, idempotency_key, reason, "
                    "source_transaction_id, idempotency_claim_id, request_fingerprint, "
                    "generation_map, affected_account_ids, affected_workspace_ids, "
                    "recompute_membership, status, phase, created_at, updated_at, "
                    "cancelled_at) VALUES "
                    "(:id, 'identity', 'actor', 'status', 'migration-backfill-key', "
                    "'retry after abort', NULL, "
                    "'00000000-0000-0000-0000-000000000921', 'retry-fingerprint', "
                    "'{}'::jsonb, '[]'::jsonb, '[]'::jsonb, false, 'CANCELLED', "
                    "'CANCELLED', now(), now(), now())"
                ),
                {"id": retry_work_id},
            )

        command.downgrade(config, "0003_authorization_source_xid")
        with authorization_owner_engine.connect() as db:
            preserved = db.execute(
                text(
                    'SELECT status, generation_map FROM "authorization".convergence_work '
                    "WHERE id=:id"
                ),
                {"id": work_id},
            ).one()
            preserved_count = db.execute(
                text(
                    'SELECT count(*) FROM "authorization".convergence_work '
                    "WHERE source_module='identity' AND actor='actor' "
                    "AND operation='status' AND idempotency_key='migration-backfill-key'"
                )
            ).scalar_one()
        assert preserved.status == "PENDING"
        assert preserved.generation_map == {account_id: 3}
        assert preserved_count == 2

        command.upgrade(config, "heads")
        with authorization_owner_engine.connect() as db:
            restored = db.execute(
                text(
                    'SELECT account_id, generation FROM "authorization".'
                    "convergence_principal_pending WHERE work_id=:id"
                ),
                {"id": work_id},
            ).one()
        assert restored == (account_id, 3)
    finally:
        command.upgrade(config, "heads")
        with authorization_owner_engine.begin() as db:
            db.execute(
                text(
                    'DELETE FROM "authorization".convergence_principal_pending '
                    "WHERE work_id IN (:id, :retry_id)"
                ),
                {"id": work_id, "retry_id": retry_work_id},
            )
            db.execute(
                text('DELETE FROM "authorization".convergence_work WHERE id IN (:id, :retry_id)'),
                {"id": work_id, "retry_id": retry_work_id},
            )
            db.execute(
                text('DELETE FROM "authorization".principal_version WHERE account_id=:account_id'),
                {"account_id": account_id},
            )


def test_authorization_schema_is_absent_before_task_9_migration(
    authorization_owner_engine: Engine,
) -> None:
    """RED: adding the migration must make the required schema/tables exist."""
    inspector = inspect(authorization_owner_engine)

    assert set(inspector.get_table_names(schema="authorization")) == {
        "convergence_principal_pending",
        "convergence_work",
        "grant",
        "idempotency_record",
        "principal_version",
        "route_registry",
    }


def test_authorization_tables_enforce_grant_and_fence_invariants(
    authorization_owner_engine: Engine,
) -> None:
    with authorization_owner_engine.connect() as db:
        transaction = db.begin()
        try:
            with pytest.raises(IntegrityError):
                db.execute(
                    text(
                        'INSERT INTO "authorization"."grant" '
                        "(id, principal_id, capability, scope_type, scope_id, source, status, "
                        "version, created_at, updated_at) VALUES "
                        "('00000000-0000-0000-0000-000000000901', 'p', 'cap', "
                        "'PLATFORM', 'not-null', 'MANUAL', 'ACTIVE', 1, now(), now())"
                    )
                )
        finally:
            transaction.rollback()


def test_convergence_work_binds_optional_claim_identity_as_an_atomic_pair(
    authorization_owner_engine: Engine,
) -> None:
    columns = {
        column["name"]
        for column in inspect(authorization_owner_engine).get_columns(
            "convergence_work",
            schema="authorization",
        )
    }
    assert {"idempotency_claim_id", "request_fingerprint"} <= columns

    with pytest.raises(IntegrityError):
        with authorization_owner_engine.begin() as db:
            db.execute(
                text(
                    'INSERT INTO "authorization".convergence_work '
                    "(id, source_module, actor, operation, idempotency_key, reason, "
                    "source_transaction_id, idempotency_claim_id, request_fingerprint, "
                    "generation_map, affected_account_ids, affected_workspace_ids, "
                    "recompute_membership, status, phase, created_at, updated_at) VALUES "
                    "('00000000-0000-0000-0000-000000000910', 'organization', "
                    "'actor', 'operation', 'key', 'reason', '1', "
                    "'00000000-0000-0000-0000-000000000911', NULL, "
                    "'{}'::jsonb, '[]'::jsonb, '[]'::jsonb, false, "
                    "'PENDING', 'SOURCE_REGISTERED', now(), now())"
                )
            )


def test_product_migration_contains_no_runtime_login_secret() -> None:
    source = Path("migrations/authorization/0001_authorization_base.py").read_text(encoding="utf-8")
    assert "LOGIN PASSWORD" not in source.upper()
    assert "'localdev'" not in source


def test_authorization_runtime_role_is_least_privilege(
    authorization_rw_engine: Engine,
) -> None:
    expected = {
        "grant": {"SELECT", "INSERT", "UPDATE"},
        "principal_version": {"SELECT", "INSERT", "UPDATE"},
        "route_registry": {"SELECT"},
        "idempotency_record": {"SELECT", "INSERT", "UPDATE"},
        "convergence_work": {"SELECT", "INSERT", "UPDATE"},
        "convergence_principal_pending": {"SELECT", "INSERT", "DELETE"},
    }
    with authorization_rw_engine.connect() as db:
        actual = {
            table_name: {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if db.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'authorization_rw', :qualified_name, :privilege)"
                    ),
                    {
                        "qualified_name": f'"authorization"."{table_name}"',
                        "privilege": privilege,
                    },
                ).scalar_one()
            }
            for table_name in expected
        }
        audit_dml = db.execute(
            text("SELECT has_table_privilege('authorization_rw', 'audit.audit_event', 'INSERT')")
        ).scalar_one()
    assert actual == expected
    assert audit_dml is False


def test_authorization_runtime_cannot_update_pending_associations(
    authorization_rw_engine: Engine,
) -> None:
    with pytest.raises(ProgrammingError):
        with authorization_rw_engine.begin() as db:
            db.execute(
                text(
                    'UPDATE "authorization".convergence_principal_pending '
                    "SET generation=generation+1"
                )
            )


@pytest.mark.parametrize(
    "statement",
    [
        (
            'INSERT INTO "authorization"."grant" '
            "(id, principal_id, capability, scope_type, scope_id, source, status, "
            "version, created_at, updated_at) VALUES "
            "('00000000-0000-0000-0000-000000000902', 'p', 'cap', "
            "'WORKSPACE', NULL, 'MANUAL', 'ACTIVE', 1, now(), now())"
        ),
        (
            'INSERT INTO "authorization"."grant" '
            "(id, principal_id, capability, scope_type, scope_id, source, status, "
            "version, created_at, updated_at, revoked_at, revoked_by, revoke_reason) "
            "VALUES ('00000000-0000-0000-0000-000000000903', 'p', 'cap', "
            "'PLATFORM', NULL, 'MANUAL', 'REVOKED', 1, now(), now(), now(), "
            "NULL, 'reason')"
        ),
        (
            'INSERT INTO "authorization"."grant" '
            "(id, principal_id, capability, scope_type, scope_id, source, status, "
            "version, created_at, updated_at, revoked_at, revoked_by, revoke_reason) "
            "VALUES ('00000000-0000-0000-0000-000000000904', 'p', 'cap', "
            "'PLATFORM', NULL, 'MANUAL', 'REVOKED', 1, now(), now(), now(), "
            "'actor', NULL)"
        ),
        (
            'INSERT INTO "authorization".principal_version '
            "(account_id, version, fence_generation, dirty_generation, dirty_reason, "
            "updated_at) VALUES ('null-dirty-reason', 1, 1, 1, NULL, now())"
        ),
        (
            'INSERT INTO "authorization".idempotency_record '
            "(id, actor, operation, idempotency_key, request_fingerprint, state, "
            "http_status, result_metadata, sealed_response, created_at, updated_at, "
            "completed_at) VALUES "
            "('00000000-0000-0000-0000-000000000905', 'actor', 'operation', "
            "'null-http-status', 'fingerprint', 'COMPLETED', NULL, '{}'::jsonb, "
            "'bytes'::bytea, now(), now(), now())"
        ),
        (
            'INSERT INTO "authorization".convergence_work '
            "(id, source_module, actor, operation, idempotency_key, reason, "
            "generation_map, affected_account_ids, affected_workspace_ids, "
            "recompute_membership, status, phase, created_at, updated_at) VALUES "
            "('00000000-0000-0000-0000-000000000906', 'identity', 'actor', "
            "'status', 'incomplete-work', 'reason', "
            "jsonb_build_object('account', NULL), "
            "'[]'::jsonb, '[]'::jsonb, true, 'PENDING', 'SOURCE_REGISTERED', "
            "now(), now())"
        ),
    ],
)
def test_authorization_runtime_role_rejects_null_incomplete_state(
    authorization_rw_engine: Engine,
    statement: str,
) -> None:
    with pytest.raises(IntegrityError):
        with authorization_rw_engine.begin() as db:
            db.execute(text(statement))
