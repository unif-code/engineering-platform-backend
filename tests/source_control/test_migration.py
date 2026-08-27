from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.integration_database import migration_database_url
from tests.source_control.conftest import IsolatedSourceControlDatabase

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "binding_request_inbox",
    "delivery_request_inbox",
    "merge_request_binding",
    "merge_request_observation",
    "repository_branch_binding",
    "source_control_effect",
    "webhook_inbox",
    "workspace_repository",
}


@pytest.fixture
def fresh_source_control_migration_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[URL]:
    owner_url = migration_database_url()
    database_name = f"test_source_control_migration_{uuid4().hex}"
    maintenance = create_engine(
        owner_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with maintenance.connect() as db:
        db.execute(text(f'CREATE DATABASE "{database_name}"'))
    target_url = owner_url.set(database=database_name)
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        target_url.render_as_string(hide_password=False),
    )
    try:
        yield target_url
    finally:
        with maintenance.connect() as db:
            db.execute(text(f'DROP DATABASE "{database_name}"'))
        maintenance.dispose()


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


def _insert_effect_graph(db: object) -> None:
    db.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO source_control.workspace_repository "
            "(id, workspace_id, provider, project_id, project_path, default_branch, "
            "connection_ref, credential_secret_ref, webhook_signing_secret_ref, status, "
            "revision) VALUES ('10000000-0000-0000-0000-000000000301', "
            "'20000000-0000-0000-0000-000000000301', 'GITLAB', '101', "
            "'platform/backend', 'main', 'gitlab-dev', 'secret-ref:credential', "
            "'secret-ref:webhook', 'AUTHORIZED', 1)"
        )
    )
    db.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO source_control.binding_request_inbox "
            "(message_id, payload_hash, requirement_id, requirement_version, work_item_id, "
            "repository_id, state, attempts) VALUES "
            "('30000000-0000-0000-0000-000000000301', 'sha256:request', "
            "'40000000-0000-0000-0000-000000000301', 1, "
            "'50000000-0000-0000-0000-000000000301', "
            "'10000000-0000-0000-0000-000000000301', 'RECEIVED', 0)"
        )
    )
    db.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO source_control.source_control_effect "
            "(id, effect_key, operation, subject_key, payload, work_item_id, "
            "requirement_id, repository_id, "
            "work_item_number, branch_name, base_commit_sha, request_fingerprint, attempts, "
            "state, requirement_callback_state) VALUES "
            "('60000000-0000-0000-0000-000000000301', 'create:work-item-301', "
            "'CREATE_TASK_BRANCH', "
            "'work-item:50000000-0000-0000-0000-000000000301', '{}'::jsonb, "
            "'50000000-0000-0000-0000-000000000301', "
            "'40000000-0000-0000-0000-000000000301', "
            "'10000000-0000-0000-0000-000000000301', 301, "
            "'feat/wi-301-source-control', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'sha256:fingerprint', 0, 'PLANNED', 'PENDING')"
        )
    )


def test_source_control_schema_and_runtime_role_exist(
    source_control_owner_engine: Engine,
) -> None:
    inspector = inspect(source_control_owner_engine)

    assert set(inspector.get_table_names(schema="source_control")) == EXPECTED_TABLES
    with source_control_owner_engine.connect() as db:
        role = db.execute(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname='source_control_rw'")
        ).scalar_one()
        sequence_exists = db.execute(
            text(
                "SELECT EXISTS (SELECT FROM pg_class c JOIN pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname='source_control' "
                "AND c.relname='work_item_number_seq' AND c.relkind='S')"
            )
        ).scalar_one()
    assert role is False
    assert sequence_exists is True


def test_source_control_owner_uniqueness_constraints_are_installed(
    source_control_owner_engine: Engine,
) -> None:
    inspector = inspect(source_control_owner_engine)

    effects = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "source_control_effect",
            schema="source_control",
        )
    }
    bindings = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "repository_branch_binding",
            schema="source_control",
        )
    }
    webhooks = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "webhook_inbox",
            schema="source_control",
        )
    }

    effect_indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspector.get_indexes(
            "source_control_effect",
            schema="source_control",
        )
    }

    assert effects["uq_source_control_effect_operation_subject"] == (
        "operation",
        "subject_key",
    )
    assert "uq_source_control_effect_work_item" not in effects
    assert effect_indexes["uq_source_control_effect_branch_number"] == ("work_item_number",)
    assert bindings["uq_source_control_binding_work_item"] == ("work_item_id",)
    assert bindings["uq_source_control_binding_branch"] == (
        "repository_id",
        "branch_name",
    )
    assert webhooks["uq_source_control_webhook_message"] == (
        "repository_id",
        "webhook_id",
    )


def _insert_integration_graph(db: object) -> None:
    _insert_effect_graph(db)
    db.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO source_control.repository_branch_binding "
            "(id, work_item_id, requirement_id, workspace_id, repository_id, "
            "work_item_number, base_commit_sha, branch_name, effect_id) VALUES "
            "('70000000-0000-0000-0000-000000000301', "
            "'50000000-0000-0000-0000-000000000301', "
            "'40000000-0000-0000-0000-000000000301', "
            "'20000000-0000-0000-0000-000000000301', "
            "'10000000-0000-0000-0000-000000000301', 301, "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'feat/wi-301-source-control', "
            "'60000000-0000-0000-0000-000000000301')"
        )
    )
    db.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO source_control.source_control_effect "
            "(id, effect_key, operation, subject_key, payload, work_item_id, "
            "requirement_id, repository_id, request_fingerprint, attempts, state, "
            "requirement_callback_state, completed_at) VALUES "
            "('60000000-0000-0000-0000-000000000302', 'create-mr:work-item-301', "
            "'CREATE_INTEGRATION_MR', "
            "'work-item:50000000-0000-0000-0000-000000000301', "
            '\'{"branchBindingId":"70000000-0000-0000-0000-000000000301",'
            '"headSha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}\'::jsonb, '
            "'50000000-0000-0000-0000-000000000301', "
            "'40000000-0000-0000-0000-000000000301', "
            "'10000000-0000-0000-0000-000000000301', "
            "'sha256:create-mr', 0, 'SUCCEEDED', 'PENDING', now())"
        )
    )
    db.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO source_control.merge_request_binding "
            "(id, kind, work_item_id, requirement_id, workspace_id, repository_id, "
            "branch_binding_id, external_project_id, merge_request_iid, source_branch, "
            "target_branch, create_effect_id, head_sha, creation_origin) VALUES "
            "('71000000-0000-0000-0000-000000000301', 'INTEGRATION', "
            "'50000000-0000-0000-0000-000000000301', "
            "'40000000-0000-0000-0000-000000000301', "
            "'20000000-0000-0000-0000-000000000301', "
            "'10000000-0000-0000-0000-000000000301', "
            "'70000000-0000-0000-0000-000000000301', '101', 42, "
            "'feat/wi-301-source-control', 'dev', "
            "'60000000-0000-0000-0000-000000000302', "
            "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'PLATFORM_CREATED')"
        )
    )
    db.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO source_control.merge_request_observation "
            "(id, binding_id, head_sha, state, observation_digest, observed_at) VALUES "
            "('80000000-0000-0000-0000-000000000301', "
            "'71000000-0000-0000-0000-000000000301', "
            "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'OPEN', "
            "'sha256:open', now())"
        )
    )


def test_product_migration_contains_no_runtime_login_secret() -> None:
    source = Path("migrations/source_control/0001_source_control_foundation.py").read_text(
        encoding="utf-8"
    )

    assert "LOGIN PASSWORD" not in source.upper()
    assert "CREATE ROLE SOURCE_CONTROL_RW LOGIN" not in source.upper()


def test_source_control_rw_has_minimum_privileges(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    expected = {
        "workspace_repository": {"SELECT", "INSERT", "UPDATE"},
        "binding_request_inbox": {"SELECT", "INSERT", "UPDATE"},
        "source_control_effect": {"SELECT", "INSERT", "UPDATE"},
        "repository_branch_binding": {"SELECT", "INSERT"},
        "webhook_inbox": {"SELECT", "INSERT", "UPDATE"},
        "delivery_request_inbox": {"SELECT", "INSERT", "UPDATE"},
        "merge_request_binding": {"SELECT", "INSERT"},
        "merge_request_observation": {"SELECT", "INSERT"},
    }
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    with isolated_source_control_database.runtime.connect() as db:
        actual = {
            table_name: {
                privilege
                for privilege in privileges
                if db.execute(
                    text(
                        "SELECT has_table_privilege('source_control_rw', "
                        "'source_control.' || :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
            }
            for table_name in expected
        }
        own_schema = db.execute(
            text("SELECT has_schema_privilege('source_control_rw','source_control','USAGE')")
        ).scalar_one()
        denied_schemas = {
            schema_name: db.execute(
                text("SELECT has_schema_privilege('source_control_rw', :schema_name, 'USAGE')"),
                {"schema_name": schema_name},
            ).scalar_one()
            for schema_name in (
                "requirement",
                "authorization",
                "identity",
                "workspace",
            )
        }
        audit_schema = db.execute(
            text("SELECT has_schema_privilege('source_control_rw','audit','USAGE')")
        ).scalar_one()
        audit_table_write = db.execute(
            text("SELECT has_table_privilege('source_control_rw','audit.audit_event','INSERT')")
        ).scalar_one()
        audit_append = db.execute(
            text(
                "SELECT has_function_privilege('source_control_rw', "
                "'audit.append_event(text,timestamptz,text,text,text,text,text,text,"
                "text,text,integer)', "
                "'EXECUTE')"
            )
        ).scalar_one()
        sequence_usage = db.execute(
            text(
                "SELECT has_sequence_privilege('source_control_rw', "
                "'source_control.work_item_number_seq','USAGE')"
            )
        ).scalar_one()

    assert actual == expected
    assert own_schema is True
    assert denied_schemas == {
        "requirement": False,
        "authorization": False,
        "identity": False,
        "workspace": False,
    }
    assert audit_schema is True
    assert audit_table_write is False
    assert audit_append is True
    assert sequence_usage is True


def test_integration_tables_columns_and_effect_shape_are_installed(
    source_control_owner_engine: Engine,
) -> None:
    inspector = inspect(source_control_owner_engine)
    effects = {
        column["name"]: column
        for column in inspector.get_columns("source_control_effect", schema="source_control")
    }
    inbox = {
        column["name"]: column
        for column in inspector.get_columns("delivery_request_inbox", schema="source_control")
    }
    observations = {
        column["name"]: column
        for column in inspector.get_columns("merge_request_observation", schema="source_control")
    }

    assert effects["subject_key"]["nullable"] is False
    assert effects["payload"]["nullable"] is False
    assert effects["work_item_number"]["nullable"] is True
    assert effects["branch_name"]["nullable"] is True
    assert effects["base_commit_sha"]["nullable"] is True
    assert inbox["attempts"]["nullable"] is False
    assert observations["merge_commit_sha"]["nullable"] is True


def test_merge_request_webhook_summary_columns_and_checks_are_installed(
    source_control_owner_engine: Engine,
) -> None:
    inspector = inspect(source_control_owner_engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("webhook_inbox", schema="source_control")
    }
    constraints = {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "webhook_inbox",
            schema="source_control",
        )
    }

    assert {
        "mr_iid",
        "mr_action",
        "source_branch",
        "target_branch",
        "mr_state",
        "old_head_sha",
        "head_sha",
    } <= columns.keys()
    assert all(
        columns[name]["nullable"] is True
        for name in (
            "mr_iid",
            "mr_action",
            "source_branch",
            "target_branch",
            "mr_state",
            "old_head_sha",
            "head_sha",
        )
    )
    assert {
        "ck_source_control_webhook_mr_shape",
        "ck_source_control_webhook_mr_refs",
    } <= constraints


def test_mr_binding_and_observation_are_append_only_for_runtime_role(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)

    with isolated_source_control_database.runtime.begin() as db:
        with pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE source_control.merge_request_binding SET target_branch='main' "
                    "WHERE id='71000000-0000-0000-0000-000000000301'"
                )
            )
    with isolated_source_control_database.runtime.begin() as db:
        with pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE source_control.merge_request_observation SET state='CLOSED' "
                    "WHERE id='80000000-0000-0000-0000-000000000301'"
                )
            )
    with isolated_source_control_database.runtime.begin() as db:
        with pytest.raises(DBAPIError):
            db.execute(
                text(
                    "DELETE FROM source_control.merge_request_observation "
                    "WHERE id='80000000-0000-0000-0000-000000000301'"
                )
            )


def test_database_rejects_literal_credentials_outside_the_reference_grammar(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.runtime.begin() as db, pytest.raises(DBAPIError):
        db.execute(
            text(
                "INSERT INTO source_control.workspace_repository "
                "(id, workspace_id, provider, project_id, project_path, default_branch, "
                "connection_ref, credential_secret_ref, status, revision) VALUES "
                "('literal-credential-repository', "
                "'20000000-0000-0000-0000-000000000399', 'GITLAB', 'literal-project', "
                "'platform/backend', 'main', 'gitlab-dev', "
                "'custom-gitlab-token-value', 'AUTHORIZED', 1)"
            )
        )


def test_branch_binding_is_immutable_for_runtime_role(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_effect_graph(db)
        db.execute(
            text(
                "INSERT INTO source_control.repository_branch_binding "
                "(id, work_item_id, requirement_id, workspace_id, repository_id, "
                "work_item_number, base_commit_sha, branch_name, effect_id) VALUES "
                "('70000000-0000-0000-0000-000000000301', "
                "'50000000-0000-0000-0000-000000000301', "
                "'40000000-0000-0000-0000-000000000301', "
                "'20000000-0000-0000-0000-000000000301', "
                "'10000000-0000-0000-0000-000000000301', 301, "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                "'feat/wi-301-source-control', "
                "'60000000-0000-0000-0000-000000000301')"
            )
        )

    with isolated_source_control_database.runtime.begin() as db, pytest.raises(DBAPIError):
        db.execute(
            text(
                "UPDATE source_control.repository_branch_binding "
                "SET branch_name='feat/wi-301-mutated' "
                "WHERE id='70000000-0000-0000-0000-000000000301'"
            )
        )


def test_effect_binding_and_webhook_uniqueness_constraints(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_effect_graph(db)
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO source_control.source_control_effect "
                    "(id, effect_key, operation, subject_key, payload, work_item_id, "
                    "requirement_id, repository_id, "
                    "work_item_number, branch_name, base_commit_sha, request_fingerprint, "
                    "attempts, state, requirement_callback_state) VALUES "
                    "('60000000-0000-0000-0000-000000000302', 'duplicate-work-item', "
                    "'CREATE_TASK_BRANCH', "
                    "'work-item:50000000-0000-0000-0000-000000000301', '{}'::jsonb, "
                    "'50000000-0000-0000-0000-000000000301', "
                    "'40000000-0000-0000-0000-000000000301', "
                    "'10000000-0000-0000-0000-000000000301', 302, "
                    "'feat/wi-302-duplicate', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                    "'sha256:duplicate', 0, 'PLANNED', 'PENDING')"
                )
            )


def test_different_effect_operations_can_share_one_work_item(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_effect_graph(db)
        db.execute(
            text(
                "INSERT INTO source_control.source_control_effect "
                "(id, effect_key, operation, subject_key, payload, work_item_id, "
                "requirement_id, repository_id, request_fingerprint, attempts, state, "
                "requirement_callback_state) VALUES "
                "('60000000-0000-0000-0000-000000000302', 'create-mr:work-item-301', "
                "'CREATE_INTEGRATION_MR', "
                "'work-item:50000000-0000-0000-0000-000000000301', "
                '\'{"branchBindingId":"70000000-0000-0000-0000-000000000301",'
                '"headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\'::jsonb, '
                "'50000000-0000-0000-0000-000000000301', "
                "'40000000-0000-0000-0000-000000000301', "
                "'10000000-0000-0000-0000-000000000301', "
                "'sha256:create-mr', 0, 'PLANNED', 'PENDING')"
            )
        )
        operations = tuple(
            db.execute(
                text(
                    "SELECT operation FROM source_control.source_control_effect "
                    "WHERE work_item_id="
                    "'50000000-0000-0000-0000-000000000301' ORDER BY operation"
                )
            ).scalars()
        )

    assert operations == ("CREATE_INTEGRATION_MR", "CREATE_TASK_BRANCH")


@pytest.mark.parametrize(
    ("operation", "subject_key", "payload"),
    [
        (
            "CREATE_INTEGRATION_MR",
            "work-item:50000000-0000-0000-0000-000000000301",
            "{}",
        ),
        (
            "CREATE_INTEGRATION_MR",
            "work-item:50000000-0000-0000-0000-000000000301",
            '{"branchBindingId":"70000000-0000-0000-0000-000000000301",'
            '"headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"projectId":"101"}',
        ),
        (
            "CREATE_INTEGRATION_MR",
            "work-item:50000000-0000-0000-0000-000000000301",
            '{"branchBindingId":"70000000-0000-0000-0000-000000000301","headSha":42}',
        ),
        (
            "MERGE_INTEGRATION_MR",
            "mr:71000000-0000-0000-0000-000000000301:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            '{"bindingId":"71000000-0000-0000-0000-000000000301"}',
        ),
        (
            "MERGE_INTEGRATION_MR",
            "mr:71000000-0000-0000-0000-000000000301:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            '{"bindingId":"71000000-0000-0000-0000-000000000301",'
            '"requestedHeadSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"token":"must-not-persist"}',
        ),
    ],
)
def test_database_rejects_non_exact_integration_effect_payloads(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    operation: str,
    subject_key: str,
    payload: str,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_effect_graph(db)
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO source_control.source_control_effect "
                    "(id, effect_key, operation, subject_key, payload, work_item_id, "
                    "requirement_id, repository_id, request_fingerprint, attempts, state, "
                    "requirement_callback_state) VALUES "
                    "('60000000-0000-0000-0000-000000000399', 'invalid-payload', "
                    ":operation, :subject_key, CAST(:payload AS JSONB), "
                    "'50000000-0000-0000-0000-000000000301', "
                    "'40000000-0000-0000-0000-000000000301', "
                    "'10000000-0000-0000-0000-000000000301', "
                    "'sha256:invalid-payload', 0, 'PLANNED', 'PENDING')"
                ),
                {
                    "operation": operation,
                    "subject_key": subject_key,
                    "payload": payload,
                },
            )


@pytest.mark.parametrize(
    "subject_key",
    [
        "mr:71000000-0000-0000-0000-000000000399:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "mr:71000000-0000-0000-0000-000000000301:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ],
)
def test_database_rejects_merge_effect_subject_payload_mismatch(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    subject_key: str,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_effect_graph(db)
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO source_control.source_control_effect "
                    "(id, effect_key, operation, subject_key, payload, work_item_id, "
                    "requirement_id, repository_id, request_fingerprint, attempts, state, "
                    "requirement_callback_state) VALUES "
                    "('60000000-0000-0000-0000-000000000398', 'mismatched-subject', "
                    "'MERGE_INTEGRATION_MR', :subject_key, "
                    '\'{"bindingId":"71000000-0000-0000-0000-000000000301",'
                    '"requestedHeadSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\''
                    "::jsonb, '50000000-0000-0000-0000-000000000301', "
                    "'40000000-0000-0000-0000-000000000301', "
                    "'10000000-0000-0000-0000-000000000301', "
                    "'sha256:mismatched-subject', 0, 'PLANNED', 'PENDING')"
                ),
                {"subject_key": subject_key},
            )


def test_0005_backfills_historical_effect_without_rewriting_identity_or_state(
    fresh_source_control_migration_database_url: URL,
) -> None:
    config = _config(fresh_source_control_migration_database_url)
    command.upgrade(config, "0004_sc_secret_reference")
    engine = create_engine(fresh_source_control_migration_database_url)
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO source_control.workspace_repository "
                    "(id, workspace_id, provider, project_id, project_path, default_branch, "
                    "connection_ref, credential_secret_ref, status, revision) VALUES "
                    "('10000000-0000-0000-0000-000000000391', "
                    "'20000000-0000-0000-0000-000000000391', 'GITLAB', '391', "
                    "'platform/history', 'main', 'gitlab-dev', "
                    "'secret-ref:credential', 'AUTHORIZED', 1)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO source_control.source_control_effect "
                    "(id, effect_key, operation, work_item_id, requirement_id, repository_id, "
                    "work_item_number, branch_name, base_commit_sha, request_fingerprint, "
                    "attempts, state, requirement_callback_state, created_at, updated_at) "
                    "VALUES ('60000000-0000-0000-0000-000000000391', 'history:391', "
                    "'CREATE_TASK_BRANCH', '50000000-0000-0000-0000-000000000391', "
                    "'40000000-0000-0000-0000-000000000391', "
                    "'10000000-0000-0000-0000-000000000391', 391, "
                    "'feat/wi-391-history', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                    "'sha256:history', 2, 'UNKNOWN', 'FAILED', "
                    "'2026-08-26T05:00:00Z', '2026-08-26T05:01:00Z')"
                )
            )

        command.upgrade(config, "source_control@head")
        with engine.connect() as db:
            upgraded = (
                db.execute(
                    text(
                        "SELECT id, subject_key, payload, state, attempts, created_at, updated_at "
                        "FROM source_control.source_control_effect "
                        "WHERE id='60000000-0000-0000-0000-000000000391'"
                    )
                )
                .mappings()
                .one()
            )
        assert str(upgraded["id"]) == "60000000-0000-0000-0000-000000000391"
        assert upgraded["subject_key"] == ("work-item:50000000-0000-0000-0000-000000000391")
        assert upgraded["payload"] == {}
        assert upgraded["state"] == "UNKNOWN"
        assert upgraded["attempts"] == 2
        assert upgraded["created_at"].isoformat() == "2026-08-26T05:00:00+00:00"
        assert upgraded["updated_at"].isoformat() == "2026-08-26T05:01:00+00:00"

        command.downgrade(config, "source_control@0004_sc_secret_reference")
        columns = {
            column["name"]
            for column in inspect(engine).get_columns(
                "source_control_effect",
                schema="source_control",
            )
        }
        with engine.connect() as db:
            restored_id = db.execute(
                text(
                    "SELECT id FROM source_control.source_control_effect "
                    "WHERE id='60000000-0000-0000-0000-000000000391'"
                )
            ).scalar_one()
        assert "subject_key" not in columns
        assert "payload" not in columns
        assert str(restored_id) == "60000000-0000-0000-0000-000000000391"
    finally:
        engine.dispose()


def test_0005_downgrade_fails_before_ddl_when_integration_facts_exist(
    fresh_source_control_migration_database_url: URL,
) -> None:
    config = _config(fresh_source_control_migration_database_url)
    command.upgrade(config, "heads")
    engine = create_engine(fresh_source_control_migration_database_url)
    try:
        with engine.begin() as db:
            _insert_integration_graph(db)

        with pytest.raises(Exception, match="integration delivery facts"):
            command.downgrade(config, "source_control@0004_sc_secret_reference")

        inspector = inspect(engine)
        assert inspector.has_table("merge_request_binding", schema="source_control")
        assert "subject_key" in {
            column["name"]
            for column in inspector.get_columns(
                "source_control_effect",
                schema="source_control",
            )
        }
    finally:
        engine.dispose()


def test_0006_preserves_historical_push_without_inventing_mr_summary(
    fresh_source_control_migration_database_url: URL,
) -> None:
    config = _config(fresh_source_control_migration_database_url)
    command.upgrade(config, "0005_sc_int_delivery")
    engine = create_engine(fresh_source_control_migration_database_url)
    try:
        with engine.begin() as db:
            _insert_effect_graph(db)
            db.execute(
                text(
                    "INSERT INTO source_control.webhook_inbox "
                    "(id, repository_id, webhook_id, webhook_timestamp, payload_digest, "
                    "event_type, object_kind, project_id, ref, before_sha, after_sha, "
                    "checkout_sha, state, processed_at) VALUES "
                    "('90000000-0000-0000-0000-000000000391', "
                    "'10000000-0000-0000-0000-000000000301', 'push-391', now(), "
                    "'sha256:push-391', 'Push Hook', 'push', '101', "
                    "'refs/heads/feat/wi-391-history', "
                    "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                    "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                    "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                    "'PROCESSED', now())"
                )
            )

        command.upgrade(config, "0006_sc_mr_reconcile")
        with engine.connect() as db:
            summary = db.execute(
                text(
                    "SELECT mr_iid, mr_action, source_branch, target_branch, mr_state, "
                    "old_head_sha, head_sha FROM source_control.webhook_inbox "
                    "WHERE webhook_id='push-391'"
                )
            ).one()

        assert summary == (None, None, None, None, None, None, None)
        command.downgrade(config, "source_control@0005_sc_int_delivery")
        assert "mr_iid" not in {
            column["name"]
            for column in inspect(engine).get_columns(
                "webhook_inbox",
                schema="source_control",
            )
        }
    finally:
        engine.dispose()


def test_0006_downgrade_refuses_to_discard_mr_webhook_summary(
    fresh_source_control_migration_database_url: URL,
) -> None:
    config = _config(fresh_source_control_migration_database_url)
    command.upgrade(config, "heads")
    engine = create_engine(fresh_source_control_migration_database_url)
    try:
        with engine.begin() as db:
            _insert_effect_graph(db)
            db.execute(
                text(
                    "INSERT INTO source_control.webhook_inbox "
                    "(id, repository_id, webhook_id, webhook_timestamp, payload_digest, "
                    "event_type, object_kind, project_id, state, processed_at, mr_iid, "
                    "mr_action, source_branch, target_branch, mr_state, head_sha) VALUES "
                    "('90000000-0000-0000-0000-000000000392', "
                    "'10000000-0000-0000-0000-000000000301', 'mr-392', now(), "
                    "'sha256:mr-392', 'Merge Request Hook', 'merge_request', '101', "
                    "'PROCESSED', now(), 17, 'merge', 'feat/wi-301-source-control', "
                    "'dev', 'merged', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')"
                )
            )

        with pytest.raises(Exception, match="MR webhook summaries"):
            command.downgrade(config, "source_control@0005_sc_int_delivery")

        assert "mr_iid" in {
            column["name"]
            for column in inspect(engine).get_columns(
                "webhook_inbox",
                schema="source_control",
            )
        }
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("mr_iid", "mr_action", "source_branch", "target_branch", "mr_state", "head_sha"),
    [
        (None, "update", "feat/wi-301-source-control", "dev", "opened", "b" * 40),
        (17, "delete", "feat/wi-301-source-control", "dev", "opened", "b" * 40),
        (17, "update", "", "dev", "opened", "b" * 40),
        (17, "update", "feat/wi-301-source-control", "", "opened", "b" * 40),
        (17, "update", "feat/wi-301-source-control", "dev", "opened", "short"),
    ],
)
def test_database_rejects_invalid_mr_webhook_summary_shape(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    mr_iid: int | None,
    mr_action: str,
    source_branch: str,
    target_branch: str,
    mr_state: str,
    head_sha: str,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_effect_graph(db)
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO source_control.webhook_inbox "
                    "(id, repository_id, webhook_id, webhook_timestamp, payload_digest, "
                    "event_type, object_kind, project_id, state, processed_at, mr_iid, "
                    "mr_action, source_branch, target_branch, mr_state, head_sha) VALUES "
                    "('90000000-0000-0000-0000-000000000399', "
                    "'10000000-0000-0000-0000-000000000301', 'mr-invalid', now(), "
                    "'sha256:mr-invalid', 'Merge Request Hook', 'merge_request', '101', "
                    "'PROCESSED', now(), :mr_iid, :mr_action, :source_branch, "
                    ":target_branch, :mr_state, :head_sha)"
                ),
                {
                    "mr_iid": mr_iid,
                    "mr_action": mr_action,
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "mr_state": mr_state,
                    "head_sha": head_sha,
                },
            )


def test_source_control_downgrade_refuses_business_rows_and_preserves_requirement(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.workspace_repository "
                "(id, workspace_id, provider, project_id, project_path, default_branch, "
                "connection_ref, credential_secret_ref, status, revision) VALUES "
                "('10000000-0000-0000-0000-000000000399', "
                "'20000000-0000-0000-0000-000000000399', 'GITLAB', '399', "
                "'platform/preserved', 'main', 'gitlab-dev', 'secret-ref:credential', "
                "'AUTHORIZED', 1)"
            )
        )

    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        isolated_source_control_database.url.render_as_string(hide_password=False).replace(
            "%", "%%"
        ),
    )
    with pytest.raises(Exception, match="business rows"):
        command.downgrade(config, "source_control@base")

    assert inspect(isolated_source_control_database.owner).has_table(
        "requirement", schema="requirement"
    )
