# ruff: noqa: E501

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from tests.agent.conftest import IsolatedAgentDatabase

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "agent_definition",
    "agent_run",
    "agent_attempt",
    "execution_binding",
    "canonical_event",
    "event_acceptance_receipt",
    "checkpoint",
    "workflow_command",
    "idempotency_key",
}

EXPECTED_UPDATE_COLUMNS = {
    "agent_definition": set(),
    "agent_run": {"latest_attempt_id", "revision", "state", "updated_at"},
    "agent_attempt": {
        "checkpoint_id",
        "event_sequence",
        "fencing_token",
        "revision",
        "runner_generation",
        "state",
        "terminal_evidence",
        "updated_at",
        "waiting_deadline",
    },
    "execution_binding": set(),
    "canonical_event": set(),
    "event_acceptance_receipt": set(),
    "checkpoint": set(),
    "workflow_command": {
        "claim_lease_until",
        "claim_mode",
        "claim_owner",
        "claim_token",
        "dispatch_attempts",
        "dispatched_at",
        "last_error_code",
        "receipt",
        "state",
        "updated_at",
    },
    "idempotency_key": {
        "completed_at",
        "http_status",
        "result_metadata",
        "sealed_response",
        "state",
        "updated_at",
    },
}


def test_agent_branch_is_independent_and_declares_required_runtime_contract() -> None:
    source = Path("migrations/agent/0001_agent_control_plane.py").read_text(encoding="utf-8")
    config = Config("alembic.ini")

    assert 'revision = "0001_agent_control_plane"' in source
    assert 'branch_labels = ("agent",)' in source
    assert "agent/dev-control-plane-probe" in source
    assert "CREATE ROLE agent_rw NOLOGIN" in source
    assert "GRANT EXECUTE ON FUNCTION audit.append_event" in source
    assert "migrations/agent" in (config.get_main_option("version_locations") or "")


def test_agent_schema_seeds_the_restricted_dev_definition_and_exact_constraints(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    inspector = inspect(isolated_agent_database.owner)
    assert set(inspector.get_table_names(schema="agent")) == EXPECTED_TABLES

    with isolated_agent_database.owner.connect() as db:
        definition = (
            db.execute(
                text(
                    "SELECT name, version, runtime_permissions FROM agent.agent_definition "
                    "WHERE name='agent/dev-control-plane-probe'"
                )
            )
            .mappings()
            .one()
        )
        role_can_login = db.execute(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname='agent_rw'")
        ).scalar_one()

    assert definition["version"] == 1
    assert definition["runtime_permissions"] == ["checkpoint.write", "context.read", "event.emit"]
    assert role_can_login is False


def test_workflow_claim_lease_migration_has_nullable_fenced_columns_and_exact_grants(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    columns = {
        str(column["name"]): column
        for column in inspect(isolated_agent_database.owner).get_columns(
            "workflow_command", schema="agent"
        )
    }
    assert {
        name: columns[name]["nullable"]
        for name in ("claim_owner", "claim_token", "claim_lease_until", "claim_mode")
    } == {
        "claim_owner": True,
        "claim_token": True,
        "claim_lease_until": True,
        "claim_mode": True,
    }
    with isolated_agent_database.runtime.connect() as db:
        granted = {
            name
            for (name,) in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='agent' AND table_name='workflow_command' "
                    "AND has_column_privilege('agent_rw', 'agent.workflow_command', "
                    "column_name, 'UPDATE')"
                )
            )
        }
    assert granted == EXPECTED_UPDATE_COLUMNS["workflow_command"]


def test_workflow_claim_lease_migration_has_exact_types_checks_index_and_fresh_heads(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with isolated_agent_database.owner.connect() as db:
        columns = {
            row["column_name"]: (
                row["data_type"],
                row["udt_name"],
                row["is_nullable"],
            )
            for row in db.execute(
                text(
                    "SELECT column_name, data_type, udt_name, is_nullable "
                    "FROM information_schema.columns WHERE table_schema='agent' "
                    "AND table_name='workflow_command' AND column_name LIKE 'claim_%'"
                )
            ).mappings()
        }
        constraints = {
            row["conname"]: row["definition"]
            for row in db.execute(
                text(
                    "SELECT con.conname, pg_get_constraintdef(con.oid) AS definition "
                    "FROM pg_constraint AS con "
                    "JOIN pg_class AS rel ON rel.oid=con.conrelid "
                    "JOIN pg_namespace AS ns ON ns.oid=rel.relnamespace "
                    "WHERE ns.nspname='agent' AND rel.relname='workflow_command' "
                    "AND con.conname LIKE 'ck_agent_command_%'"
                )
            ).mappings()
        }
        index_definition = db.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname='agent' "
                "AND tablename='workflow_command' "
                "AND indexname='ix_agent_command_claim_lease'"
            )
        ).scalar_one()
        installed_heads = {
            row[0] for row in db.execute(text("SELECT version_num FROM alembic_version"))
        }

    assert columns == {
        "claim_lease_until": ("timestamp with time zone", "timestamptz", "YES"),
        "claim_mode": ("text", "text", "YES"),
        "claim_owner": ("text", "text", "YES"),
        "claim_token": ("uuid", "uuid", "YES"),
    }
    assert "ck_agent_command_claim" in constraints
    assert {
        "ck_agent_command_claim",
        "ck_agent_command_claim_attempt_limit",
        "ck_agent_command_claim_mode_state",
        "ck_agent_command_outcome_evidence",
    } <= constraints.keys()
    claim_check = constraints["ck_agent_command_claim"]
    assert all(
        fragment in claim_check
        for fragment in (
            "claim_owner IS NULL",
            "claim_token IS NULL",
            "claim_lease_until IS NULL",
            "claim_mode IS NULL",
            "length(btrim(claim_owner)) > 0",
            "claim_mode = ANY",
            "'DISPATCH'::text",
            "'RECONCILE'::text",
        )
    )
    assert index_definition == (
        "CREATE INDEX ix_agent_command_claim_lease ON agent.workflow_command USING btree "
        "(claim_lease_until, created_at, id) WHERE (claim_token IS NOT NULL)"
    )
    script_heads = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
    assert installed_heads <= script_heads
    assert "0004_run_business_context" in installed_heads
    assert "0001_agent_control_plane" not in installed_heads


def test_workflow_claim_lease_upgrade_normalizes_predecessor_valid_rows(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "agent@0001_agent_control_plane")
    predecessor_rows = (
        (
            "10000000-0000-0000-0000-000000009961",
            "predecessor-unknown-ack",
            "UNKNOWN",
            1,
            '{"commandKey":"predecessor-unknown-ack","outcome":"ACKNOWLEDGEMENT_UNKNOWN"}',
            "DEV_ACKNOWLEDGEMENT_UNKNOWN",
            None,
        ),
        (
            "10000000-0000-0000-0000-000000009962",
            "predecessor-claimed-crash",
            "UNKNOWN",
            2,
            '{"commandKey":"predecessor-claimed-crash","outcome":"CLAIMED"}',
            None,
            None,
        ),
        (
            "10000000-0000-0000-0000-000000009963",
            "predecessor-pre-call",
            "PLANNED",
            2,
            None,
            "DEV_PRE_CALL_FAILURE",
            None,
        ),
        (
            "10000000-0000-0000-0000-000000009964",
            "predecessor-high-attempt-dispatched",
            "DISPATCHED",
            4,
            '{"commandKey":"predecessor-high-attempt-dispatched","outcome":"ACCEPTED"}',
            None,
            "2026-08-31T08:00:00+00:00",
        ),
        (
            "10000000-0000-0000-0000-000000009965",
            "predecessor-exhausted-pre-call",
            "PLANNED",
            4,
            None,
            "DEV_PRE_CALL_FAILURE",
            None,
        ),
        (
            "10000000-0000-0000-0000-000000009966",
            "predecessor-third-claim-crash",
            "UNKNOWN",
            3,
            '{"commandKey":"predecessor-third-claim-crash","outcome":"CLAIMED"}',
            None,
            None,
        ),
        (
            "10000000-0000-0000-0000-000000009967",
            "predecessor-over-limit-claim-crash",
            "UNKNOWN",
            5,
            '{"commandKey":"predecessor-over-limit-claim-crash","outcome":"CLAIMED"}',
            None,
            None,
        ),
    )
    with isolated_agent_database.owner.begin() as db:
        for row in predecessor_rows:
            db.execute(
                text(
                    "INSERT INTO agent.workflow_command "
                    "(id, command_key, kind, attempt_id, generation, state, dispatch_attempts, "
                    "receipt, last_error_code, dispatched_at, created_at, updated_at) VALUES "
                    "(CAST(:id AS UUID), :command_key, 'START', "
                    "'20000000-0000-0000-0000-000000009961', 1, :state, :attempts, "
                    "CAST(:receipt AS JSONB), :error_code, CAST(:dispatched_at AS TIMESTAMPTZ), "
                    "'2026-08-31T08:00:00+00:00', '2026-08-31T08:00:00+00:00')"
                ),
                {
                    "id": row[0],
                    "command_key": row[1],
                    "state": row[2],
                    "attempts": row[3],
                    "receipt": row[4],
                    "error_code": row[5],
                    "dispatched_at": row[6],
                },
            )

    command.upgrade(config, "agent@head")

    with isolated_agent_database.owner.connect() as db:
        normalized = {
            row["command_key"]: (
                row["state"],
                row["dispatch_attempts"],
                row["receipt"],
                row["last_error_code"],
                row["claim_owner"],
                row["claim_token"],
                row["claim_lease_until"],
                row["claim_mode"],
            )
            for row in db.execute(
                text(
                    "SELECT command_key, state, dispatch_attempts, receipt, last_error_code, "
                    "claim_owner, claim_token, claim_lease_until, claim_mode "
                    "FROM agent.workflow_command ORDER BY command_key"
                )
            ).mappings()
        }
    assert normalized == {
        "predecessor-claimed-crash": (
            "UNKNOWN",
            2,
            None,
            "WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN",
            "migration-0002-predecessor-claimed",
            UUID("10000000-0000-0000-0000-000000009962"),
            datetime(1970, 1, 1, tzinfo=UTC),
            "RECONCILE",
        ),
        "predecessor-exhausted-pre-call": (
            "UNKNOWN",
            3,
            None,
            "WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED",
            None,
            None,
            None,
            None,
        ),
        "predecessor-high-attempt-dispatched": (
            "DISPATCHED",
            3,
            {
                "commandKey": "predecessor-high-attempt-dispatched",
                "outcome": "ACCEPTED",
            },
            None,
            None,
            None,
            None,
            None,
        ),
        "predecessor-over-limit-claim-crash": (
            "UNKNOWN",
            3,
            None,
            "WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED",
            "migration-0002-predecessor-claimed",
            UUID("10000000-0000-0000-0000-000000009967"),
            datetime(1970, 1, 1, tzinfo=UTC),
            "RECONCILE",
        ),
        "predecessor-pre-call": (
            "PLANNED",
            2,
            None,
            "WORKFLOW_PRE_CALL_FAILURE",
            None,
            None,
            None,
            None,
        ),
        "predecessor-third-claim-crash": (
            "UNKNOWN",
            3,
            None,
            "WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED",
            "migration-0002-predecessor-claimed",
            UUID("10000000-0000-0000-0000-000000009966"),
            datetime(1970, 1, 1, tzinfo=UTC),
            "RECONCILE",
        ),
        "predecessor-unknown-ack": (
            "UNKNOWN",
            1,
            None,
            "WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN",
            None,
            None,
            None,
            None,
        ),
    }
    with pytest.raises(DBAPIError):
        with isolated_agent_database.owner.begin() as db:
            db.execute(
                text(
                    "UPDATE agent.workflow_command SET receipt="
                    '\'{"commandKey":"predecessor-unknown-ack","outcome":"CLAIMED"}\' '
                    "WHERE command_key='predecessor-unknown-ack'"
                )
            )


@pytest.mark.parametrize(
    "invalid_set_clause",
    [
        "state='DISPATCHED', receipt=NULL, dispatched_at=now(), last_error_code=NULL",
        'state=\'DISPATCHED\', receipt=\'{"commandKey":"wrong","outcome":"ACCEPTED"}\', '
        "dispatched_at=now(), last_error_code=NULL",
        'state=\'DISPATCHED\', receipt=\'{"commandKey":"workflow-guard",'
        '"outcome":"REJECTED"}\', dispatched_at=now(), last_error_code=NULL',
        'state=\'DISPATCHED\', receipt=\'{"commandKey":"workflow-guard",'
        '"outcome":"ACCEPTED","claimToken":"must-not-persist"}\', '
        "dispatched_at=now(), last_error_code=NULL",
        'state=\'UNKNOWN\', receipt=\'{"commandKey":"workflow-guard",'
        '"outcome":"ACCEPTED"}\', dispatched_at=NULL, '
        "last_error_code='WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN'",
        "state='UNKNOWN', receipt=NULL, dispatched_at=NULL, "
        "last_error_code='SENSITIVE_EXCEPTION_SENTINEL'",
        "dispatch_attempts=4",
        "claim_owner='partial-claim'",
        "state='UNKNOWN', receipt=NULL, dispatched_at=NULL, "
        "last_error_code='WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN', dispatch_attempts=1, "
        "claim_owner='wrong-mode', claim_token='30000000-0000-0000-0000-000000009942', "
        "claim_lease_until=now() + interval '30 seconds', claim_mode='DISPATCH'",
    ],
)
def test_workflow_command_database_rejects_invalid_outcome_evidence_and_attempts(
    isolated_agent_database: IsolatedAgentDatabase,
    invalid_set_clause: str,
) -> None:
    with isolated_agent_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO agent.workflow_command "
                "(id, command_key, kind, attempt_id, generation, state) VALUES "
                "('10000000-0000-0000-0000-000000009941', 'workflow-guard', 'START', "
                "'20000000-0000-0000-0000-000000009941', 1, 'PLANNED')"
            )
        )

    with pytest.raises(DBAPIError):
        with isolated_agent_database.owner.begin() as db:
            db.execute(
                text(
                    "UPDATE agent.workflow_command SET "
                    + invalid_set_clause
                    + " WHERE command_key='workflow-guard'"
                )
            )


def test_agent_runtime_role_is_exact_and_cannot_mutate_immutable_evidence(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    tables = {
        "agent_definition": {"SELECT", "INSERT"},
        "agent_run": {"SELECT", "INSERT"},
        "agent_attempt": {"SELECT", "INSERT"},
        "execution_binding": {"SELECT", "INSERT"},
        "canonical_event": {"SELECT", "INSERT"},
        "checkpoint": {"SELECT", "INSERT"},
        "workflow_command": {"SELECT", "INSERT"},
        "idempotency_key": {"SELECT", "INSERT"},
    }
    with isolated_agent_database.runtime.connect() as db:
        actual = {
            table: {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if db.execute(
                    text(
                        "SELECT has_table_privilege('agent_rw', "
                        "'agent.' || :table_name, :privilege)"
                    ),
                    {"table_name": table, "privilege": privilege},
                ).scalar_one()
            }
            for table in tables
        }
        audit_append = db.execute(
            text(
                "SELECT has_function_privilege('agent_rw', "
                "'audit.append_event(text,timestamptz,text,text,text,text,text,text,"
                "text,text,integer)', 'EXECUTE')"
            )
        ).scalar_one()
        actual_update_columns = {
            table: {
                column_name
                for (column_name,) in db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='agent' AND table_name=:table_name "
                        "AND has_column_privilege('agent_rw', "
                        "'agent.' || :table_name, column_name, 'UPDATE')"
                    ),
                    {"table_name": table},
                )
            }
            for table in EXPECTED_TABLES
        }
        audit_table_privileges = {
            privilege: db.execute(
                text("SELECT has_table_privilege('agent_rw', 'audit.audit_event', :privilege)"),
                {"privilege": privilege},
            ).scalar_one()
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            )
        }

    assert actual == tables
    assert actual_update_columns == EXPECTED_UPDATE_COLUMNS
    assert audit_append is True
    assert audit_table_privileges == {
        "SELECT": False,
        "INSERT": False,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
        "REFERENCES": False,
        "TRIGGER": False,
    }


def test_agent_downgrade_refuses_to_discard_business_rows(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with isolated_agent_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO agent.agent_run "
                "(id, workspace_id, goal_ref, created_by, definition_id, definition_version, "
                "latest_attempt_id, state, revision) "
                "SELECT '10000000-0000-0000-0000-000000000801', "
                "'20000000-0000-0000-0000-000000000801', 'goal:801', 'employee-801', id, "
                "version, '30000000-0000-0000-0000-000000000801', 'ACTIVE', 1 "
                "FROM agent.agent_definition WHERE name='agent/dev-control-plane-probe'"
            )
        )

    with pytest.raises(Exception, match="refusing Agent downgrade"):
        command.downgrade(Config("alembic.ini"), "agent@base")

    assert inspect(isolated_agent_database.owner).has_table("agent_run", schema="agent")
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM agent.agent_run")).scalar_one() == 1


def test_workflow_claim_lease_downgrade_refuses_a_live_claim(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with isolated_agent_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO agent.workflow_command "
                "(id, command_key, kind, attempt_id, generation, state, dispatch_attempts, "
                "claim_owner, claim_token, claim_lease_until, claim_mode) VALUES "
                "('10000000-0000-0000-0000-000000009951', 'live-claim', 'START', "
                "'20000000-0000-0000-0000-000000009951', 1, 'PLANNED', 1, "
                "'live-dispatcher', '30000000-0000-0000-0000-000000009951', "
                "now() + interval '30 seconds', 'DISPATCH')"
            )
        )

    with pytest.raises(Exception, match="refusing Agent claim-lease downgrade: live claims"):
        command.downgrade(Config("alembic.ini"), "agent@0001_agent_control_plane")

    columns = {
        str(column["name"])
        for column in inspect(isolated_agent_database.owner).get_columns(
            "workflow_command", schema="agent"
        )
    }
    assert {"claim_owner", "claim_token", "claim_lease_until", "claim_mode"} <= columns
    with isolated_agent_database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM agent.workflow_command WHERE claim_token IS NOT NULL")
            ).scalar_one()
            == 1
        )


def test_agent_runtime_role_cannot_delete_seeded_definition(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    with pytest.raises(DBAPIError):
        with isolated_agent_database.runtime.begin() as db:
            db.execute(
                text(
                    "DELETE FROM agent.agent_definition WHERE name='agent/dev-control-plane-probe'"
                )
            )


def test_agent_database_constraints_reject_invalid_platform_facts(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    invalid_statements = (
        "INSERT INTO agent.agent_run (id, workspace_id, goal_ref, created_by, definition_id, "
        "definition_version, latest_attempt_id, state, revision) VALUES "
        "('10000000-0000-0000-0000-000000000901', '20000000-0000-0000-0000-000000000901', "
        "'goal:901', 'employee-901', '30000000-0000-0000-0000-000000000901', 1, "
        "'40000000-0000-0000-0000-000000000901', 'INVALID', 1)",
        "INSERT INTO agent.agent_attempt (id, run_id, number, state, binding_id, binding_digest, "
        "runner_generation, fencing_token, revision) VALUES "
        "('10000000-0000-0000-0000-000000000902', '20000000-0000-0000-0000-000000000902', "
        "1, 'QUEUED', '30000000-0000-0000-0000-000000000902', 'bad', 1, 'fence', 1)",
        "INSERT INTO agent.checkpoint (id, attempt_id, artifact_id, artifact_version, content_sha256, "
        "schema_version, adapter_version, classification) VALUES "
        "('10000000-0000-0000-0000-000000000903', '20000000-0000-0000-0000-000000000903', "
        "'artifact', '1', 'sha256:" + "a" * 64 + "', '1', '1', 'TOP_SECRET')",
        "INSERT INTO agent.agent_run (id, workspace_id, goal_ref, created_by, definition_id, "
        "definition_version, latest_attempt_id, state, revision, created_at, updated_at) VALUES "
        "('10000000-0000-0000-0000-000000000904', '20000000-0000-0000-0000-000000000904', "
        "'goal:904', 'employee-904', '30000000-0000-0000-0000-000000000904', 1, "
        "'40000000-0000-0000-0000-000000000904', 'ACTIVE', 1, now(), now() - interval '1 second')",
    )
    for statement in invalid_statements:
        with pytest.raises(DBAPIError):
            with isolated_agent_database.owner.begin() as db:
                db.execute(text(statement))

    with pytest.raises(DBAPIError):
        with isolated_agent_database.owner.begin() as db:
            db.execute(
                text(
                    "INSERT INTO agent.agent_run (id, workspace_id, goal_ref, created_by, "
                    "definition_id, definition_version, latest_attempt_id, state, revision) VALUES "
                    "('not-a-uuid', '20000000-0000-0000-0000-000000000905', 'goal:905', "
                    "'employee-905', '30000000-0000-0000-0000-000000000905', 1, "
                    "'40000000-0000-0000-0000-000000000905', 'ACTIVE', 1)"
                )
            )


def test_run_source_migration_preserves_predecessor_null_without_guessing_goal_ref(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
    from control_plane.app.modules.agent.domain import RunMutation, RunState
    from tests.agent.test_repository import (
        ATTEMPT,
        BINDING,
        DEFINITION,
        RUN,
        insert_predecessor_run,
    )

    config = Config("alembic.ini")
    command.downgrade(config, "agent@0003_event_acceptance_receipt")
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        insert_predecessor_run(db)
        repository.insert_attempt(ATTEMPT)
        repository.insert_binding(ATTEMPT.id, BINDING)
    with isolated_agent_database.owner.connect() as db:
        original = dict(db.execute(text("SELECT * FROM agent.agent_run")).mappings().one())
    command.upgrade(config, "heads")
    with isolated_agent_database.runtime.begin() as db:
        row = dict(db.execute(text("SELECT * FROM agent.agent_run")).mappings().one())
        assert {key: row[key] for key in original} == original
        assert [row[key] for key in ("requirement_id", "work_item_id", "assignment_id")] == [
            None,
            None,
            None,
        ]
        repository = SqlAlchemyAgentRepository(db)
        legacy = repository.run_by_id(RUN.id)
        assert legacy is not None and legacy.business_context is None
        updated = repository.compare_and_set_run(
            RUN.id,
            expected_revision=1,
            mutation=RunMutation(
                state=RunState.ACTIVE,
                latest_attempt_id=ATTEMPT.id,
                now=RUN.updated_at,
            ),
        )
        assert updated is not None and updated.business_context is None and updated.revision == 2


def test_run_source_columns_reject_partial_facts_and_runtime_updates_or_deletion(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
    from control_plane.app.modules.agent.domain import RunMutation, RunState
    from tests.agent.test_repository import RUN, _seed

    database = isolated_agent_database
    with database.runtime.begin() as db:
        _seed(SqlAlchemyAgentRepository(db))
    with database.owner.connect() as db:
        source_columns = db.execute(
            text(
                "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
                "WHERE table_schema='agent' AND table_name='agent_run' "
                "AND column_name IN ('requirement_id','work_item_id','assignment_id')"
            )
        ).all()
    assert set(source_columns) == {
        (name, "uuid", "YES") for name in ("requirement_id", "work_item_id", "assignment_id")
    }
    for column in ("requirement_id", "work_item_id", "assignment_id"):
        with pytest.raises(DBAPIError, match="permission denied"):
            with database.runtime.begin() as db:
                db.execute(text(f"UPDATE agent.agent_run SET {column}={column}"))
    with pytest.raises(DBAPIError, match="permission denied"):
        with database.runtime.begin() as db:
            db.execute(text("DELETE FROM agent.agent_run"))
    with pytest.raises(DBAPIError, match="ck_agent_run_business_context"):
        with database.owner.begin() as db:
            db.execute(text("UPDATE agent.agent_run SET assignment_id=NULL"))
    with database.runtime.begin() as db:
        updated = SqlAlchemyAgentRepository(db).compare_and_set_run(
            RUN.id,
            expected_revision=1,
            mutation=RunMutation(
                state=RunState.ACTIVE,
                latest_attempt_id=RUN.latest_attempt_id,
                now=RUN.updated_at,
            ),
        )
        assert updated is not None and updated.business_context == RUN.business_context
        assert updated.revision == 2
    with pytest.raises(DBAPIError, match="business source snapshots"):
        command.downgrade(Config("alembic.ini"), "agent@0003_event_acceptance_receipt")
    with database.runtime.connect() as db:
        assert SqlAlchemyAgentRepository(db).run_by_id(RUN.id) == updated
