import re
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.requirement.domain.formal import (
    FormalDeliveryBlockedReason,
)
from tests.integration_database import migration_database_url
from tests.requirement.test_migration import EXPECTED_TABLES

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_requirement_database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    owner_url = migration_database_url()
    database_name = f"test_requirement_migration_{uuid4().hex}"
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


def _insert_formal_migration_subject(
    db: Connection,
    *,
    formal_state: str,
    blocked_reason: str | None,
    touched: bool,
    subject_number: int = 701,
) -> None:
    requirement_id = f"10000000-0000-0000-0000-{subject_number:012d}"
    workspace_id = f"20000000-0000-0000-0000-{subject_number:012d}"
    work_item_id = f"30000000-0000-0000-0000-{subject_number:012d}"
    db.execute(
        text(
            "INSERT INTO requirement.requirement "
            "(id, workspace_id, type, title, description, acceptance_criteria, "
            "acceptance_criteria_version, acceptance_criteria_hash, created_by, "
            "initial_repository_id, route_snapshot_version, route_snapshot_hash, "
            "route_snapshot, state, record_state, requirement_version, "
            "required_work_item_set_version, required_work_item_set_hash, revision) VALUES "
            "(:requirement_id, :workspace_id, 'feat', 'Formal migration', "
            "'Formal downgrade guard', '[\"accepted\"]'::jsonb, 1, :criteria_hash, "
            "'employee-1', 'repository-1', 1, :route_hash, CAST(:route_snapshot AS JSONB), "
            "'READY', 'ACTIVE', 1, 1, :set_hash, 1)"
        ),
        {
            "requirement_id": requirement_id,
            "workspace_id": workspace_id,
            "criteria_hash": "sha256:" + "a" * 64,
            "route_hash": "sha256:" + "b" * 64,
            "route_snapshot": (
                '{"requirementType":"feat","requiredCapabilities":["code.change"],"version":1}'
            ),
            "set_hash": "sha256:" + "c" * 64,
        },
    )
    db.execute(
        text(
            "INSERT INTO requirement.work_item "
            "(id, requirement_id, created_by, human_owner_id, executor_type, executor_id, "
            "required_capabilities, assignment_state, repository_state, state, "
            "repository_id, base_commit_sha, task_branch, integration_delivery_state, "
            "formal_delivery_state, formal_blocked_reason_code, formal_updated_at, revision) "
            "VALUES (:work_item_id, :requirement_id, 'employee-1', 'employee-1', "
            "'HUMAN', 'employee-1', '[\"code.change\"]'::jsonb, 'ASSIGNED', 'BOUND', "
            "'READY', 'repository-1', :base_sha, 'feat/formal-migration', 'NOT_STARTED', "
            ":formal_state, :blocked_reason, "
            "CASE WHEN :touched THEN now() ELSE NULL END, 1)"
        ),
        {
            "base_sha": "d" * 40,
            "formal_state": formal_state,
            "blocked_reason": blocked_reason,
            "touched": touched,
            "work_item_id": work_item_id,
            "requirement_id": requirement_id,
        },
    )


def test_fresh_upgrade_installs_requirement_and_all_visible_heads(
    fresh_requirement_database_url: URL,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "heads")
    engine = create_engine(fresh_requirement_database_url)
    try:
        expected_heads = set(ScriptDirectory.from_config(config).get_heads())
        with engine.connect() as db:
            installed_heads = set(
                db.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
        assert expected_heads == {
            "0007_model_material_sources",
            "0003_agent_run_recovery",
            "0010_audit_model_worker_grant",
            "0011_identity_reauth_consumption",
            "0001_organization_base",
            "0001_workspace_base",
            "0012_auth_agent_control",
            "0009_req_formal_owner_denial",
            "0003_event_acceptance_receipt",
            "0011_sc_delivery_join",
        }
        assert installed_heads == {
            "0007_model_material_sources",
            "0003_agent_run_recovery",
            "0010_audit_model_worker_grant",
            "0011_identity_reauth_consumption",
            "0012_auth_agent_control",
            "0009_req_formal_owner_denial",
            "0003_event_acceptance_receipt",
            "0011_sc_delivery_join",
        }
        assert set(inspect(engine).get_table_names(schema="requirement")) == EXPECTED_TABLES
    finally:
        engine.dispose()


def test_requirement_0002_upgrades_the_original_0001_schema_in_place(
    fresh_requirement_database_url: URL,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "0001_requirement_base")
    engine = create_engine(fresh_requirement_database_url)
    try:
        before = {
            column["name"]
            for column in inspect(engine).get_columns("requirement", schema="requirement")
        }
        assert "current_sdd_baseline_id" not in before

        command.upgrade(config, "heads")

        after = {
            column["name"]
            for column in inspect(engine).get_columns("requirement", schema="requirement")
        }
        with engine.connect() as db:
            constraints = set(
                db.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE connamespace='requirement'::regnamespace"
                    )
                ).scalars()
            )
            gate_table_update = db.execute(
                text(
                    "SELECT has_table_privilege("
                    "'requirement_rw', 'requirement.gate_instance', 'UPDATE')"
                )
            ).scalar_one()
            gate_state_update = db.execute(
                text(
                    "SELECT has_column_privilege("
                    "'requirement_rw', 'requirement.gate_instance', 'state', 'UPDATE')"
                )
            ).scalar_one()
        assert "current_sdd_baseline_id" in after
        assert {
            "fk_requirement_current_sdd_baseline",
            "uq_requirement_sdd_owner",
            "ck_requirement_work_item_repository",
        } <= constraints
        assert gate_table_update is False
        assert gate_state_update is True
    finally:
        engine.dispose()


def test_requirement_0006_exact_delivery_subject_constraints_survive_round_trip(
    fresh_requirement_database_url: URL,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "requirement@0006_req_evidence_acceptance")
    engine = create_engine(fresh_requirement_database_url)

    def assert_exact_subject_schema() -> None:
        inspector = inspect(engine)
        snapshot_unique = {
            item["name"]: item["column_names"]
            for item in inspector.get_unique_constraints(
                "requirement_delivery_snapshot", schema="requirement"
            )
        }
        selection_unique = {
            item["name"]: item["column_names"]
            for item in inspector.get_unique_constraints(
                "integration_baseline_selection", schema="requirement"
            )
        }
        selection_foreign_keys = {
            item["name"]: (item["constrained_columns"], item["referred_columns"])
            for item in inspector.get_foreign_keys(
                "integration_baseline_selection", schema="requirement"
            )
        }
        gate_foreign_keys = {
            item["name"]: (item["constrained_columns"], item["referred_columns"])
            for item in inspector.get_foreign_keys("delivery_gate", schema="requirement")
        }
        selection_columns = {
            item["name"]
            for item in inspector.get_columns(
                "integration_baseline_selection", schema="requirement"
            )
        }

        assert "delivery_snapshot_hash" in selection_columns
        assert snapshot_unique["uq_req_snapshot_exact_subject"] == [
            "id",
            "requirement_id",
            "requirement_version",
            "required_work_item_set_version",
            "required_work_item_set_hash",
            "snapshot_hash",
        ]
        assert selection_foreign_keys["fk_req_selection_snapshot"] == (
            [
                "delivery_snapshot_id",
                "requirement_id",
                "evidence_requirement_version",
                "evidence_required_work_item_set_version",
                "evidence_required_work_item_set_hash",
                "delivery_snapshot_hash",
            ],
            [
                "id",
                "requirement_id",
                "requirement_version",
                "required_work_item_set_version",
                "required_work_item_set_hash",
                "snapshot_hash",
            ],
        )
        assert selection_unique["uq_req_selection_exact_subject"] == [
            "id",
            "requirement_id",
            "requirement_version_after",
            "integration_baseline_id",
            "integration_baseline_hash",
        ]
        assert gate_foreign_keys["fk_req_delivery_gate_selection"] == (
            [
                "selection_id",
                "requirement_id",
                "requirement_version",
                "integration_baseline_id",
                "integration_baseline_hash",
            ],
            [
                "id",
                "requirement_id",
                "requirement_version_after",
                "integration_baseline_id",
                "integration_baseline_hash",
            ],
        )

    try:
        assert_exact_subject_schema()

        command.downgrade(config, "requirement@0005_req_sdd_human_gate")
        assert "integration_baseline_selection" not in inspect(engine).get_table_names(
            schema="requirement"
        )

        command.upgrade(config, "requirement@0006_req_evidence_acceptance")
        assert_exact_subject_schema()
    finally:
        engine.dispose()


def test_requirement_0007_preserves_completed_v05_delivery_facts(
    fresh_requirement_database_url: URL,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "requirement@0006_req_evidence_acceptance")
    engine = create_engine(fresh_requirement_database_url)
    requirement_id = "10000000-0000-0000-0000-000000000706"
    work_item_id = "30000000-0000-0000-0000-000000000706"
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO requirement.requirement "
                    "(id, workspace_id, type, title, description, acceptance_criteria, "
                    "acceptance_criteria_version, acceptance_criteria_hash, created_by, "
                    "initial_repository_id, route_snapshot_version, route_snapshot_hash, "
                    "route_snapshot, state, record_state, requirement_version, "
                    "required_work_item_set_version, required_work_item_set_hash, revision) "
                    "VALUES (:requirement_id, "
                    "'20000000-0000-0000-0000-000000000706', 'feat', "
                    "'Completed before V0.6', 'Preserve released V0.5 delivery facts', "
                    "'[\"accepted\"]'::jsonb, 1, :criteria_hash, 'employee-1', "
                    "'repository-1', 1, :route_hash, "
                    "CAST(:route_snapshot AS JSONB), "
                    "'COMPLETED', 'ACTIVE', 1, 1, :set_hash, 1)"
                ),
                {
                    "requirement_id": requirement_id,
                    "criteria_hash": "sha256:" + "a" * 64,
                    "route_hash": "sha256:" + "b" * 64,
                    "route_snapshot": (
                        '{"requirementType":"feat","requiredCapabilities":'
                        '["code.change"],"version":1}'
                    ),
                    "set_hash": "sha256:" + "c" * 64,
                },
            )
            db.execute(
                text(
                    "INSERT INTO requirement.work_item "
                    "(id, requirement_id, created_by, human_owner_id, executor_type, "
                    "executor_id, required_capabilities, assignment_state, repository_state, "
                    "state, repository_id, base_commit_sha, task_branch, "
                    "integration_delivery_state, integration_merge_request_binding_id, "
                    "integration_updated_at, revision) VALUES "
                    "(:work_item_id, :requirement_id, 'employee-1', 'employee-1', "
                    "'HUMAN', 'employee-1', '[\"code.change\"]'::jsonb, 'ASSIGNED', "
                    "'BOUND', 'COMPLETED', 'repository-1', :base_sha, "
                    "'task/completed-before-v06', 'INTEGRATED', "
                    "'97000000-0000-0000-0000-000000000706', now(), 1)"
                ),
                {
                    "work_item_id": work_item_id,
                    "requirement_id": requirement_id,
                    "base_sha": "d" * 40,
                },
            )

        command.upgrade(config, "requirement@0007_req_formal_delivery")

        with engine.connect() as db:
            preserved = db.execute(
                text(
                    "SELECT state, formal_delivery_state, "
                    "formal_merge_request_binding_id IS NULL "
                    "FROM requirement.work_item WHERE id=:work_item_id"
                ),
                {"work_item_id": work_item_id},
            ).one()
        assert tuple(preserved) == ("COMPLETED", "NOT_STARTED", True)
    finally:
        engine.dispose()


def test_all_migrations_round_trip_with_requirement_schema(
    fresh_requirement_database_url: URL,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "heads")
    command.downgrade(config, "base")
    engine = create_engine(fresh_requirement_database_url)
    try:
        assert "requirement" not in inspect(engine).get_schema_names()
        command.upgrade(config, "heads")
        assert set(inspect(engine).get_table_names(schema="requirement")) == EXPECTED_TABLES
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("fact_column", "blocked_reason", "touched"),
    [
        ("formal_updated_at", None, True),
        ("formal_blocked_reason_code", "HEAD_SHA_CHANGED", False),
    ],
)
def test_requirement_0007_downgrade_preserves_not_started_formal_facts(
    fresh_requirement_database_url: URL,
    fact_column: str,
    blocked_reason: str | None,
    touched: bool,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "requirement@0007_req_formal_delivery")
    engine = create_engine(fresh_requirement_database_url)
    try:
        with engine.begin() as db:
            if fact_column == "formal_blocked_reason_code":
                # Exercise the downgrade guard independently of the normal state CHECK.
                db.execute(
                    text(
                        "ALTER TABLE requirement.work_item "
                        "DROP CONSTRAINT ck_req_work_item_formal_block"
                    )
                )
            _insert_formal_migration_subject(
                db,
                formal_state="NOT_STARTED",
                blocked_reason=blocked_reason,
                touched=touched,
            )
            if fact_column == "formal_blocked_reason_code":
                db.execute(
                    text(
                        "ALTER TABLE requirement.work_item "
                        "ADD CONSTRAINT ck_req_work_item_formal_block "
                        "CHECK (true) NOT VALID"
                    )
                )

        with pytest.raises(Exception, match="formal delivery facts"):
            command.downgrade(config, "requirement@0006_req_evidence_acceptance")

        columns = {
            column["name"]
            for column in inspect(engine).get_columns("work_item", schema="requirement")
        }
        assert "formal_updated_at" in columns
        with engine.connect() as db:
            row = db.execute(
                text(
                    f"SELECT formal_delivery_state, "
                    f"formal_merge_request_binding_id IS NULL, {fact_column} IS NOT NULL "
                    "FROM requirement.work_item "
                    "WHERE id='30000000-0000-0000-0000-000000000701'"
                )
            ).one()
        assert row == ("NOT_STARTED", True, True)
    finally:
        engine.dispose()


def test_requirement_formal_blocked_reason_is_a_closed_public_enum(
    fresh_requirement_database_url: URL,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "requirement@head")
    engine = create_engine(fresh_requirement_database_url)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as db:
                _insert_formal_migration_subject(
                    db,
                    formal_state="BLOCKED",
                    blocked_reason="UNPUBLISHED_REASON",
                    touched=True,
                )

        with engine.begin() as db:
            for offset, reason in enumerate(FormalDeliveryBlockedReason, start=701):
                _insert_formal_migration_subject(
                    db,
                    formal_state="BLOCKED",
                    blocked_reason=reason.value,
                    touched=True,
                    subject_number=offset,
                )
        with engine.connect() as db:
            stored_reasons = set(
                db.execute(
                    text(
                        "SELECT formal_blocked_reason_code FROM requirement.work_item "
                        "WHERE formal_blocked_reason_code IS NOT NULL"
                    )
                ).scalars()
            )
            constraint_definition = db.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname='ck_req_work_item_formal_block'"
                )
            ).scalar_one()
        public_reasons = {reason.value for reason in FormalDeliveryBlockedReason}
        constraint_reasons = set(re.findall(r"'([A-Z][A-Z_]*)'", constraint_definition)) - {
            "BLOCKED"
        }
        assert stored_reasons == public_reasons
        assert constraint_reasons == public_reasons
    finally:
        engine.dispose()


def test_requirement_delivery_facts_prevent_requirement_downgrade(
    fresh_requirement_database_url: URL,
) -> None:
    config = _config(fresh_requirement_database_url)
    command.upgrade(config, "heads")
    engine = create_engine(fresh_requirement_database_url)
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO requirement.requirement "
                    "(id, workspace_id, type, title, description, acceptance_criteria, "
                    "acceptance_criteria_version, acceptance_criteria_hash, created_by, "
                    "initial_repository_id, route_snapshot_version, "
                    "route_snapshot_hash, route_snapshot, state, record_state, "
                    "requirement_version, "
                    "required_work_item_set_version, required_work_item_set_hash, revision) VALUES "
                    "('10000000-0000-0000-0000-000000000301', "
                    "'20000000-0000-0000-0000-000000000301', 'feat', 'Title', 'Description', "
                    "'[\"accepted\"]', 1, 'sha256:" + "a" * 64 + "', "
                    "'employee-1', 'repository-1', 1, 'sha256:route', "
                    '\'{"requirementType":"feat","requiredCapabilities":'
                    '["code.change"],"version": 1}\', '
                    "'IN_PROGRESS', 'ACTIVE', 1, 1, 'sha256:set', 1)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO requirement.work_item "
                    "(id, requirement_id, created_by, human_owner_id, executor_type, "
                    "executor_id, required_capabilities, assignment_state, repository_state, "
                    "state, repository_id, "
                    "base_commit_sha, task_branch, integration_delivery_state, "
                    "formal_delivery_state, revision) VALUES "
                    "('10000000-0000-0000-0000-000000000302', "
                    "'10000000-0000-0000-0000-000000000301', 'employee-1', 'employee-1', "
                    "'HUMAN', 'employee-1', '[\"code.change\"]', 'ASSIGNED', 'BOUND', "
                    "'IN_PROGRESS', 'repository-1', 'sha256:base', 'task-branch', "
                    "'IMPLEMENTING', 'NOT_STARTED', 1)"
                )
            )

        with pytest.raises(Exception, match="integration delivery facts"):
            command.downgrade(config, "requirement@0003_req_sc_relay")
    finally:
        engine.dispose()
