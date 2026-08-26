from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.source_control.conftest import IsolatedSourceControlDatabase

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "binding_request_inbox",
    "repository_branch_binding",
    "source_control_effect",
    "webhook_inbox",
    "workspace_repository",
}


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
            "(id, effect_key, operation, work_item_id, requirement_id, repository_id, "
            "work_item_number, branch_name, base_commit_sha, request_fingerprint, attempts, "
            "state, requirement_callback_state) VALUES "
            "('60000000-0000-0000-0000-000000000301', 'create:work-item-301', "
            "'CREATE_TASK_BRANCH', '50000000-0000-0000-0000-000000000301', "
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

    assert effects["uq_source_control_effect_work_item"] == ("work_item_id",)
    assert bindings["uq_source_control_binding_work_item"] == ("work_item_id",)
    assert bindings["uq_source_control_binding_branch"] == (
        "repository_id",
        "branch_name",
    )
    assert webhooks["uq_source_control_webhook_message"] == (
        "repository_id",
        "webhook_id",
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
        requirement_schema = db.execute(
            text("SELECT has_schema_privilege('source_control_rw','requirement','USAGE')")
        ).scalar_one()
        sequence_usage = db.execute(
            text(
                "SELECT has_sequence_privilege('source_control_rw', "
                "'source_control.work_item_number_seq','USAGE')"
            )
        ).scalar_one()

    assert actual == expected
    assert own_schema is True
    assert requirement_schema is False
    assert sequence_usage is True


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
                    "(id, effect_key, operation, work_item_id, requirement_id, repository_id, "
                    "work_item_number, branch_name, base_commit_sha, request_fingerprint, "
                    "attempts, state, requirement_callback_state) VALUES "
                    "('60000000-0000-0000-0000-000000000302', 'duplicate-work-item', "
                    "'CREATE_TASK_BRANCH', '50000000-0000-0000-0000-000000000301', "
                    "'40000000-0000-0000-0000-000000000301', "
                    "'10000000-0000-0000-0000-000000000301', 302, "
                    "'feat/wi-302-duplicate', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                    "'sha256:duplicate', 0, 'PLANNED', 'PENDING')"
                )
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
        isolated_source_control_database.url.replace("%", "%%"),
    )
    with pytest.raises(Exception, match="business rows"):
        command.downgrade(config, "source_control@base")

    assert inspect(isolated_source_control_database.owner).has_table(
        "requirement", schema="requirement"
    )
