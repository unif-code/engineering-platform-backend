from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from control_plane.app.modules.requirement import RepositoryBindingBlockedReason
from control_plane.app.shared.db.settings import DbSettings
from tests.requirement.conftest import IsolatedRequirementDatabase

pytestmark = pytest.mark.integration


EXPECTED_TABLES = {
    "decision",
    "gate_assignment",
    "gate_instance",
    "idempotency_record",
    "outbox_message",
    "requirement",
    "sdd_baseline",
    "work_item",
}


def _insert_requirement_for_integrity_test(db: Connection) -> None:
    db.execute(
        text(
            "INSERT INTO requirement.requirement "
            "(id, workspace_id, type, title, description, acceptance_criteria, "
            "created_by, initial_repository_id, route_snapshot_version, "
            "route_snapshot_hash, state, record_state, requirement_version, "
            "required_work_item_set_version, required_work_item_set_hash, revision) VALUES "
            "('10000000-0000-0000-0000-000000000201', "
            "'20000000-0000-0000-0000-000000000201', 'feat', 'Title', 'Description', "
            "'[\"accepted\"]', 'employee-1', 'repository-1', 1, 'sha256:route', "
            "'PREPARING', 'ACTIVE', 1, 1, 'sha256:set', 1)"
        )
    )
    db.execute(
        text(
            "INSERT INTO requirement.sdd_baseline "
            "(id, requirement_id, requirement_version, artifact_id, artifact_version, "
            "artifact_hash, route_snapshot_version, route_snapshot_hash, created_by) VALUES "
            "('10000000-0000-0000-0000-000000000202', "
            "'10000000-0000-0000-0000-000000000201', 1, 'artifact-1', 'version-1', "
            "'sha256:artifact-1', 1, 'sha256:route', 'employee-1')"
        )
    )


def _insert_gate(db: Connection, gate_id: str) -> None:
    db.execute(
        text(
            "INSERT INTO requirement.gate_instance "
            "(id, gate_type, requirement_id, requirement_version, sdd_baseline_id, "
            "artifact_id, artifact_version, artifact_hash, route_snapshot_version, "
            "route_snapshot_hash, policy_version, state, revision) VALUES "
            "(:id, 'REQUIREMENT_BASELINE_CONFIRMATION', "
            "'10000000-0000-0000-0000-000000000201', 1, "
            "'10000000-0000-0000-0000-000000000202', 'artifact-1', 'version-1', "
            "'sha256:artifact-1', 1, 'sha256:route', 1, 'OPEN', 1)"
        ),
        {"id": gate_id},
    )


def test_requirement_schema_tables_and_nologin_runtime_role_exist(
    requirement_owner_engine: Engine,
) -> None:
    inspector = inspect(requirement_owner_engine)

    assert set(inspector.get_table_names(schema="requirement")) == EXPECTED_TABLES
    with requirement_owner_engine.connect() as db:
        can_login = db.execute(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname='requirement_rw'")
        ).scalar_one()
    assert can_login is False


def test_requirement_product_migration_contains_no_runtime_login_secret() -> None:
    source = Path("migrations/requirement/0001_requirement_base.py").read_text(encoding="utf-8")

    assert "LOGIN PASSWORD" not in source.upper()
    assert "'localdev'" not in source


def test_requirement_constraints_reject_invalid_aggregate_and_work_item_facts(
    requirement_owner_engine: Engine,
) -> None:
    with requirement_owner_engine.connect() as db:
        transaction = db.begin()
        try:
            with pytest.raises(IntegrityError):
                db.execute(
                    text(
                        "INSERT INTO requirement.requirement "
                        "(id, workspace_id, type, title, description, acceptance_criteria, "
                        "created_by, initial_repository_id, route_snapshot_version, "
                        "route_snapshot_hash, state, record_state, requirement_version, "
                        "required_work_item_set_version, required_work_item_set_hash, revision) "
                        "VALUES ('10000000-0000-0000-0000-000000000001', "
                        "'20000000-0000-0000-0000-000000000001', 'story', 'Title', "
                        "'Description', '[\"accepted\"]', 'employee-1', 'repository-1', 1, "
                        "'sha256:route', 'CREATED', 'ACTIVE', 1, 1, 'sha256:set', 1)"
                    )
                )
        finally:
            transaction.rollback()


def test_repository_binding_blocked_state_requires_a_structured_reason_and_timestamp(
    requirement_owner_engine: Engine,
) -> None:
    inspector = inspect(requirement_owner_engine)
    columns = {item["name"] for item in inspector.get_columns("work_item", schema="requirement")}
    with requirement_owner_engine.connect() as db:
        constraint = db.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_requirement_work_item_repository'"
            )
        ).scalar_one()

    assert {"repository_blocked_reason_code", "repository_blocked_at"} <= columns
    assert "BLOCKED" in constraint
    assert "repository_blocked_reason_code" in constraint
    assert "repository_blocked_at" in constraint


def test_requirement_integration_delivery_columns_and_checks_exist(
    requirement_owner_engine: Engine,
) -> None:
    columns = {
        column["name"]: column
        for column in inspect(requirement_owner_engine).get_columns(
            "work_item", schema="requirement"
        )
    }
    with requirement_owner_engine.connect() as db:
        state_constraint = db.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_requirement_work_item_state'"
            )
        ).scalar_one()

    assert columns["integration_delivery_state"]["nullable"] is False
    assert columns["integration_merge_request_binding_id"]["nullable"] is True
    assert columns["integration_blocked_reason_code"]["nullable"] is True
    assert columns["integration_updated_at"]["nullable"] is True
    assert "IN_PROGRESS" in state_constraint
    assert "VERIFYING" in state_constraint


@pytest.mark.parametrize(
    "reason",
    [
        "OWNER_UNASSIGNED",
        "OWNER_INELIGIBLE",
        "REPOSITORY_NOT_AUTHORIZED",
        "RECONCILIATION_PENDING",
    ],
)
def test_source_control_blocked_reasons_are_valid_requirement_facts(
    isolated_requirement_rw_engine: Engine,
    reason: str,
) -> None:
    reason_code = RepositoryBindingBlockedReason(reason)
    with isolated_requirement_rw_engine.begin() as db:
        _insert_requirement_for_integrity_test(db)
        db.execute(
            text(
                "INSERT INTO requirement.work_item "
                "(id, requirement_id, created_by, human_owner_id, executor_type, "
                "executor_id, required_capabilities, assignment_state, repository_state, "
                "state, repository_id, base_commit_sha, task_branch, "
                "repository_blocked_reason_code, repository_blocked_at, revision) VALUES "
                "('10000000-0000-0000-0000-000000000211', "
                "'10000000-0000-0000-0000-000000000201', 'employee-1', 'employee-1', "
                "'HUMAN', 'employee-1', '[\"code.change\"]', 'ASSIGNED', 'BLOCKED', "
                "'DRAFT', 'repository-1', NULL, NULL, :reason, now(), 1)"
            ),
            {"reason": reason_code.value},
        )


def test_gate_cannot_claim_artifact_different_from_its_sdd_baseline(
    isolated_requirement_rw_engine: Engine,
) -> None:
    with isolated_requirement_rw_engine.begin() as db:
        _insert_requirement_for_integrity_test(db)
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO requirement.gate_instance "
                    "(id, gate_type, requirement_id, requirement_version, sdd_baseline_id, "
                    "artifact_id, artifact_version, artifact_hash, route_snapshot_version, "
                    "route_snapshot_hash, policy_version, state, revision) VALUES "
                    "('10000000-0000-0000-0000-000000000203', "
                    "'REQUIREMENT_BASELINE_CONFIRMATION', "
                    "'10000000-0000-0000-0000-000000000201', 1, "
                    "'10000000-0000-0000-0000-000000000202', 'artifact-1', 'version-1', "
                    "'sha256:different', 1, 'sha256:route', 1, 'OPEN', 1)"
                )
            )


def test_current_sdd_baseline_must_belong_to_the_same_requirement(
    isolated_requirement_rw_engine: Engine,
) -> None:
    with isolated_requirement_rw_engine.begin() as db:
        _insert_requirement_for_integrity_test(db)
        db.execute(
            text(
                "INSERT INTO requirement.requirement "
                "(id, workspace_id, type, title, description, acceptance_criteria, "
                "created_by, initial_repository_id, route_snapshot_version, "
                "route_snapshot_hash, state, record_state, requirement_version, "
                "required_work_item_set_version, required_work_item_set_hash, revision) "
                "VALUES ('10000000-0000-0000-0000-000000000209', "
                "'20000000-0000-0000-0000-000000000209', 'feat', 'Other', "
                "'Other requirement', '[\"accepted\"]', 'employee-2', 'repository-2', 1, "
                "'sha256:route-2', 'PREPARING', 'ACTIVE', 1, 1, 'sha256:set-2', 1)"
            )
        )
        db.execute(
            text(
                "INSERT INTO requirement.sdd_baseline "
                "(id, requirement_id, requirement_version, artifact_id, artifact_version, "
                "artifact_hash, route_snapshot_version, route_snapshot_hash, created_by) "
                "VALUES ('10000000-0000-0000-0000-000000000210', "
                "'10000000-0000-0000-0000-000000000209', 1, 'artifact-2', 'version-1', "
                "'sha256:artifact-2', 1, 'sha256:route-2', 'employee-2')"
            )
        )
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "UPDATE requirement.requirement SET current_sdd_baseline_id="
                    "'10000000-0000-0000-0000-000000000210' "
                    "WHERE id='10000000-0000-0000-0000-000000000201'"
                )
            )


def test_decision_assignment_must_belong_to_the_same_gate(
    isolated_requirement_rw_engine: Engine,
) -> None:
    gate_one = "10000000-0000-0000-0000-000000000204"
    gate_two = "10000000-0000-0000-0000-000000000205"
    assignment_one = "10000000-0000-0000-0000-000000000206"
    assignment_two = "10000000-0000-0000-0000-000000000207"
    with isolated_requirement_rw_engine.begin() as db:
        _insert_requirement_for_integrity_test(db)
        _insert_gate(db, gate_one)
        _insert_gate(db, gate_two)
        db.execute(
            text(
                "INSERT INTO requirement.gate_assignment "
                "(id, gate_instance_id, default_reviewer_id, current_reviewer_id, revision) "
                "VALUES (:one, :gate_one, 'reviewer-1', 'reviewer-1', 1), "
                "(:two, :gate_two, 'reviewer-2', 'reviewer-2', 1)"
            ),
            {
                "one": assignment_one,
                "two": assignment_two,
                "gate_one": gate_one,
                "gate_two": gate_two,
            },
        )
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO requirement.decision "
                    "(id, gate_instance_id, gate_assignment_id, reviewer_id, outcome, reason, "
                    "subject_revision, decided_at) VALUES "
                    "('10000000-0000-0000-0000-000000000208', :gate_one, :assignment_two, "
                    "'reviewer-2', 'APPROVED', 'wrong gate assignment', 1, now())"
                ),
                {"gate_one": gate_one, "assignment_two": assignment_two},
            )


def test_requirement_rw_has_only_expected_module_and_audit_privileges(
    isolated_requirement_rw_engine: Engine,
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    expected = {
        "requirement": {"SELECT", "INSERT", "UPDATE"},
        "work_item": {"SELECT", "INSERT", "UPDATE"},
        "sdd_baseline": {"SELECT", "INSERT"},
        "gate_instance": {"SELECT", "INSERT"},
        "gate_assignment": {"SELECT", "INSERT"},
        "decision": {"SELECT", "INSERT"},
        "idempotency_record": {"SELECT", "INSERT", "UPDATE"},
        "outbox_message": {"SELECT", "INSERT", "UPDATE"},
    }
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    with isolated_requirement_rw_engine.connect() as db:
        actual = {
            table_name: {
                privilege
                for privilege in privileges
                if db.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'requirement_rw', 'requirement.' || :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
            }
            for table_name in expected
        }
        schema_privileges = {
            privilege
            for privilege in ("USAGE", "CREATE")
            if db.execute(
                text("SELECT has_schema_privilege('requirement_rw', 'requirement', :privilege)"),
                {"privilege": privilege},
            ).scalar_one()
        }
        column_updates = {
            table_name: {
                column_name
                for column_name in db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='requirement' AND table_name=:table_name"
                    ),
                    {"table_name": table_name},
                ).scalars()
                if db.execute(
                    text(
                        "SELECT has_column_privilege("
                        "'requirement_rw', 'requirement.' || :table_name, "
                        ":column_name, 'UPDATE')"
                    ),
                    {"table_name": table_name, "column_name": column_name},
                ).scalar_one()
            }
            for table_name in ("gate_instance", "gate_assignment")
        }
    with isolated_requirement_database.owner.connect() as db:
        cross_module = {
            table_name: db.execute(
                text("SELECT has_table_privilege('requirement_rw', :table_name, 'SELECT')"),
                {"table_name": table_name},
            ).scalar_one()
            for table_name in ("identity.account", "workspace.workspace", "audit.audit_event")
        }
        audit_execute = db.execute(
            text(
                "SELECT has_function_privilege("
                "'requirement_rw', "
                "'audit.append_event(text,timestamptz,text,text,text,text,text,text,"
                "text,text,integer)', 'EXECUTE')"
            )
        ).scalar_one()

    assert actual == expected
    assert schema_privileges == {"USAGE"}
    assert column_updates == {
        "gate_instance": {"state", "revision", "decided_at"},
        "gate_assignment": {"superseded_at"},
    }
    assert cross_module == {
        "identity.account": False,
        "workspace.workspace": False,
        "audit.audit_event": False,
    }
    assert audit_execute is True


def test_requirement_rw_cannot_delete_aggregate_or_create_tables(
    requirement_rw_engine: Engine,
) -> None:
    with (
        requirement_rw_engine.connect() as db,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        db.execute(text("DELETE FROM requirement.requirement WHERE false"))
    with (
        requirement_rw_engine.connect() as db,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        db.execute(text("CREATE TABLE requirement.runtime_ddl_forbidden (id int)"))


def test_requirement_runtime_settings_have_a_distinct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "postgresql+psycopg://requirement_rw:localdev@127.0.0.1:55432/platform"
    monkeypatch.setenv("REQUIREMENT_DATABASE_URL", expected)

    settings = DbSettings()

    assert settings.requirement_database_url == expected
    assert settings.requirement_database_url not in {
        settings.database_url,
        settings.identity_database_url,
        settings.organization_database_url,
        settings.workspace_database_url,
        settings.authorization_database_url,
        settings.configuration_database_url,
        settings.migration_database_url,
    }
