from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from control_plane.app.shared.db.settings import DbSettings
from tests.agent_run.conftest import IsolatedAgentRunDatabase

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "capacity_ledger",
    "capacity_lease",
    "command_receipt",
    "evidence_reference",
    "preview_intent",
    "reconciliation_run",
    "runtime_state",
    "runner_generation",
    "sandbox_environment",
    "sandbox_materialization",
}

FORBIDDEN_SCHEMA_TERMS = {
    "api_key",
    "cloud",
    "credential",
    "kata",
    "kubernetes",
    "node",
    "pod",
    "provider",
    "region",
    "runtimeclass",
    "secret_value",
    "sku",
}


def test_agent_run_schema_role_tables_and_safe_columns_exist(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
) -> None:
    inspector = inspect(isolated_agent_run_database.owner)

    assert set(inspector.get_table_names(schema="agent_run")) == EXPECTED_TABLES
    columns = {
        f"{table}.{column['name']}".lower()
        for table in EXPECTED_TABLES
        for column in inspector.get_columns(table, schema="agent_run")
    }
    assert not any(term in column for term in FORBIDDEN_SCHEMA_TERMS for column in columns)
    assert "runner_generation.fencing_token_digest" in columns
    assert "sandbox_materialization.cleanup_terminal_state" in columns
    assert "sandbox_materialization.recovery_capsule" in columns
    assert "sandbox_materialization.cancellation_reason" in columns
    assert "command_receipt.owner_id" in columns
    assert "command_receipt.owner_expires_at" in columns
    assert "command_receipt.phase" in columns
    assert "preview_intent.result_capsule" in columns
    assert all("fencing_token" not in column or column.endswith("_digest") for column in columns)

    with isolated_agent_run_database.owner.connect() as db:
        can_login = db.execute(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname='agent_run_rw'")
        ).scalar_one()
    assert can_login is False


def test_active_execution_indexes_and_cleanup_constraints_are_installed(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
) -> None:
    with isolated_agent_run_database.owner.connect() as db:
        index_definitions = {
            name: definition
            for name, definition in db.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='agent_run'")
            )
        }
        constraints = {
            name: definition
            for name, definition in db.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE connamespace='agent_run'::regnamespace"
                )
            )
        }

    assert (
        "WHERE (state = 'ACTIVE'::text)" in index_definitions["uq_agent_run_active_lease_execution"]
    )
    assert (
        "WHERE (state = 'ACTIVE'::text)"
        in index_definitions["uq_agent_run_active_generation_execution"]
    )
    assert "WHERE (state = ANY" in index_definitions["uq_agent_run_active_materialization"]
    cleanup = constraints["ck_agent_run_materialization_cleanup_order"]
    assert "fenced_at IS NULL) OR (evidence_persisted_at IS NOT NULL" in cleanup
    assert "secret_revoked_at" in cleanup
    assert "lease_released_at" in cleanup
    assert "destroyed_at" in cleanup
    command = constraints["ck_agent_run_command_state"]
    assert "owner_id IS NOT NULL" in command
    assert "owner_expires_at IS NOT NULL" in command


def test_runtime_role_has_only_owned_mutation_and_audit_append_privileges(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
) -> None:
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    with isolated_agent_run_database.runtime.connect() as db:
        actual = {
            table: {
                privilege
                for privilege in privileges
                if db.execute(
                    text(
                        "SELECT has_table_privilege('agent_run_rw', "
                        "'agent_run.' || :table, :privilege)"
                    ),
                    {"table": table, "privilege": privilege},
                ).scalar_one()
            }
            for table in EXPECTED_TABLES
        }
        own_schema = db.execute(
            text("SELECT has_schema_privilege('agent_run_rw','agent_run','USAGE')")
        ).scalar_one()
        audit_append = db.execute(
            text(
                "SELECT has_function_privilege('agent_run_rw', "
                "'audit.append_event(text,timestamptz,text,text,text,text,text,text,"
                "text,text,integer)', 'EXECUTE')"
            )
        ).scalar_one()
        mutable_columns = {
            table: {
                column
                for column in db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='agent_run' AND table_name=:table"
                    ),
                    {"table": table},
                ).scalars()
                if db.execute(
                    text(
                        "SELECT has_column_privilege('agent_run_rw', "
                        "'agent_run.' || :table, :column, 'UPDATE')"
                    ),
                    {"table": table, "column": column},
                ).scalar_one()
            }
            for table in EXPECTED_TABLES
        }
    with isolated_agent_run_database.owner.connect() as db:
        cross_module = {
            table: db.execute(
                text("SELECT has_table_privilege('agent_run_rw', :table, 'SELECT')"),
                {"table": table},
            ).scalar_one()
            for table in (
                "identity.account",
                "requirement.requirement",
                "source_control.workspace_repository",
                "audit.audit_event",
            )
        }

    assert actual == {table: {"SELECT", "INSERT"} for table in EXPECTED_TABLES}
    assert mutable_columns["sandbox_environment"] == {"state", "revision", "updated_at"}
    assert mutable_columns["evidence_reference"] == set()
    assert "binding_digest" not in mutable_columns["sandbox_materialization"]
    assert "boundary_manifest" not in mutable_columns["sandbox_materialization"]
    assert "fencing_token_digest" not in mutable_columns["runner_generation"]
    assert {
        "evidence_persisted_at",
        "fenced_at",
        "secret_revoked_at",
        "lease_released_at",
        "destroyed_at",
        "cleanup_terminal_state",
    } <= mutable_columns["sandbox_materialization"]
    assert own_schema is True
    assert cross_module == {
        "identity.account": False,
        "requirement.requirement": False,
        "source_control.workspace_repository": False,
        "audit.audit_event": False,
    }
    assert audit_append is True


def test_runtime_role_cannot_delete_create_or_update_frozen_binding(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
) -> None:
    with isolated_agent_run_database.runtime.begin() as db:
        db.execute(
            text(
                "INSERT INTO agent_run.sandbox_environment "
                "(id, workspace_id, requirement_id, trust_tier, state, revision) VALUES "
                "('10000000-0000-0000-0000-000000000901', 'workspace-1', "
                "'requirement-1', 'LAB_ONLY', 'ACTIVE', 1)"
            )
        )
    with (
        isolated_agent_run_database.runtime.begin() as db,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        db.execute(
            text(
                "UPDATE agent_run.sandbox_environment SET workspace_id='workspace-2' "
                "WHERE id='10000000-0000-0000-0000-000000000901'"
            )
        )
    with (
        isolated_agent_run_database.runtime.begin() as db,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        db.execute(text("DELETE FROM agent_run.sandbox_environment WHERE false"))
    with (
        isolated_agent_run_database.runtime.begin() as db,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        db.execute(text("CREATE TABLE agent_run.runtime_ddl_forbidden (id int)"))


def test_capacity_and_cleanup_constraints_reject_invalid_facts(
    isolated_agent_run_database: IsolatedAgentRunDatabase,
) -> None:
    with isolated_agent_run_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO agent_run.sandbox_environment "
                "(id, workspace_id, requirement_id, trust_tier, state, revision) VALUES "
                "('10000000-0000-0000-0000-000000000902', 'workspace-2', "
                "'requirement-2', 'LAB_ONLY', 'ACTIVE', 1)"
            )
        )
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO agent_run.capacity_ledger "
                    "(environment_id, policy_version, policy_enabled, active_attempt_limit, "
                    "maximum_units, active_attempts, active_units, revision) VALUES "
                    "('10000000-0000-0000-0000-000000000902', 'policy-v1', true, "
                    "1, 1, 2, 2, 1)"
                )
            )


def test_agent_run_migration_contains_no_runtime_login_or_secret() -> None:
    source = Path("migrations/agent_run/0001_sandbox_runtime_base.py").read_text(encoding="utf-8")

    assert "LOGIN PASSWORD" not in source.upper()
    assert "CREATE ROLE AGENT_RUN_RW LOGIN" not in source.upper()
    assert "LOCALDEV" not in source.upper()


def test_agent_run_runtime_settings_have_a_distinct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "postgresql+psycopg://agent_run_rw:test-only@127.0.0.1:55439/platform"
    monkeypatch.setenv("AGENT_RUN_DATABASE_URL", expected)

    settings = DbSettings()

    assert settings.agent_run_database_url == expected
    assert settings.agent_run_database_url not in {
        settings.database_url,
        settings.identity_database_url,
        settings.organization_database_url,
        settings.workspace_database_url,
        settings.authorization_database_url,
        settings.configuration_database_url,
        settings.requirement_database_url,
        settings.source_control_database_url,
        settings.migration_database_url,
    }
