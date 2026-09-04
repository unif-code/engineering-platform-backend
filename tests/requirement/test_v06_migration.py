import json

import pytest
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.requirement.domain.evidence import (
    canonical_acceptance_criteria_hash,
)
from tests.requirement.conftest import IsolatedRequirementDatabase

pytestmark = pytest.mark.integration


V06_TABLES = {
    "requirement_delivery_snapshot",
    "integration_baseline_selection",
    "delivery_gate",
    "delivery_gate_assignment",
    "delivery_decision",
}


def _insert_requirement(db: Connection) -> None:
    criteria = ["Exact Evidence is accepted."]
    db.execute(
        text(
            "INSERT INTO requirement.requirement "
            "(id, workspace_id, type, title, description, acceptance_criteria, "
            "acceptance_criteria_version, acceptance_criteria_hash, created_by, "
            "initial_repository_id, route_snapshot_version, route_snapshot_hash, "
            "route_snapshot, state, record_state, requirement_version, "
            "required_work_item_set_version, required_work_item_set_hash, revision) VALUES "
            "('10000000-0000-0000-0000-000000000601', "
            "'20000000-0000-0000-0000-000000000601', 'feat', 'V0.6', 'Evidence flow', "
            "CAST(:criteria AS JSONB), 1, :criteria_hash, 'employee-1', 'repository-1', "
            "2, :route_hash, CAST(:route_snapshot AS JSONB), "
            "'VERIFYING', 'ACTIVE', 7, 3, :set_hash, 4)"
        ),
        {
            "criteria": json.dumps(criteria),
            "criteria_hash": canonical_acceptance_criteria_hash(criteria),
            "route_hash": "sha256:" + "a" * 64,
            "route_snapshot": json.dumps(
                {
                    "requirementType": "feat",
                    "requiredCapabilities": ["code.change"],
                    "steps": [],
                    "version": 2,
                },
                separators=(",", ":"),
            ),
            "set_hash": "sha256:" + "b" * 64,
        },
    )


def _insert_delivery_snapshot(db: Connection) -> None:
    db.execute(
        text(
            "INSERT INTO requirement.requirement_delivery_snapshot "
            "(id, requirement_id, requirement_version, required_work_item_set_version, "
            "required_work_item_set_hash, work_item_ids, snapshot_hash, created_by) VALUES "
            "('30000000-0000-0000-0000-000000000601', "
            "'10000000-0000-0000-0000-000000000601', 7, 3, :set_hash, "
            "'[\"20000000-0000-0000-0000-000000000601\"]'::jsonb, :snapshot_hash, "
            "'employee-1')"
        ),
        {"set_hash": "sha256:" + "b" * 64, "snapshot_hash": "sha256:" + "c" * 64},
    )


def _insert_selection(db: Connection, **overrides: object) -> None:
    values = {
        "evidence_hash": "sha256:" + "d" * 64,
        "evidence_requirement_version": 7,
        "evidence_set_hash": "sha256:" + "b" * 64,
        "evidence_set_version": 3,
        "integration_baseline_id": "50000000-0000-0000-0000-000000000601",
        "requirement_version_after": 8,
        "requirement_version_before": 7,
        "snapshot_hash": "sha256:" + "c" * 64,
        "selection_id": "40000000-0000-0000-0000-000000000601",
        **overrides,
    }
    db.execute(
        text(
            "INSERT INTO requirement.integration_baseline_selection "
            "(id, requirement_id, delivery_snapshot_id, delivery_snapshot_hash, "
            "integration_baseline_id, integration_baseline_hash, evidence_requirement_version, "
            "evidence_required_work_item_set_version, "
            "evidence_required_work_item_set_hash, requirement_version_before, "
            "requirement_version_after, selected_by) VALUES "
            "(:selection_id, '10000000-0000-0000-0000-000000000601', "
            "'30000000-0000-0000-0000-000000000601', :snapshot_hash, "
            ":integration_baseline_id, "
            ":evidence_hash, :evidence_requirement_version, :evidence_set_version, "
            ":evidence_set_hash, :requirement_version_before, "
            ":requirement_version_after, 'employee-1')"
        ),
        values,
    )


def _insert_acceptance_gate(db: Connection, **overrides: object) -> None:
    values = {
        "integration_baseline_hash": "sha256:" + "d" * 64,
        "integration_baseline_id": "50000000-0000-0000-0000-000000000601",
        "requirement_version": 8,
        **overrides,
    }
    db.execute(
        text(
            "INSERT INTO requirement.delivery_gate "
            "(id, gate_type, requirement_id, work_item_id, selection_id, "
            "requirement_version, acceptance_criteria_version, acceptance_criteria_hash, "
            "integration_baseline_id, integration_baseline_hash, "
            "formal_merge_request_binding_id, subject_head_sha, policy_code, "
            "policy_version, policy_snapshot_hash, state, revision) VALUES "
            "('60000000-0000-0000-0000-000000000601', 'REQUIREMENT_ACCEPTANCE', "
            "'10000000-0000-0000-0000-000000000601', NULL, "
            "'40000000-0000-0000-0000-000000000601', :requirement_version, 1, "
            ":criteria_hash, :integration_baseline_id, :integration_baseline_hash, NULL, "
            "NULL, 'REQUIREMENT_ACCEPTANCE', 1, :policy_hash, 'OPEN', 1)"
        ),
        {
            **values,
            "criteria_hash": canonical_acceptance_criteria_hash(["Exact Evidence is accepted."]),
            "policy_hash": "sha256:" + "e" * 64,
        },
    )


def test_v06_requirement_schema_and_delivery_states_exist(
    requirement_owner_engine: Engine,
) -> None:
    inspector = inspect(requirement_owner_engine)
    tables = set(inspector.get_table_names(schema="requirement"))
    columns = {item["name"] for item in inspector.get_columns("requirement", schema="requirement")}
    work_item_columns = {
        item["name"] for item in inspector.get_columns("work_item", schema="requirement")
    }
    selection_columns = {
        item["name"]
        for item in inspector.get_columns("integration_baseline_selection", schema="requirement")
    }
    formal_gate_index = next(
        item
        for item in inspector.get_indexes("delivery_gate", schema="requirement")
        if item["name"] == "uq_req_formal_gate_binding"
    )
    with requirement_owner_engine.connect() as db:
        requirement_state = db.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_requirement_state'"
            )
        ).scalar_one()
        work_item_state = db.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_requirement_work_item_state'"
            )
        ).scalar_one()
        formal_delivery_state = db.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_req_work_item_formal_delivery_state'"
            )
        ).scalar_one()

    assert V06_TABLES <= tables
    assert {
        "acceptance_criteria_version",
        "acceptance_criteria_hash",
        "current_integration_baseline_selection_id",
        "current_acceptance_gate_id",
    } <= columns
    assert {
        "formal_delivery_state",
        "formal_merge_request_binding_id",
        "formal_blocked_reason_code",
        "formal_updated_at",
    } <= work_item_columns
    assert "delivery_snapshot_hash" in selection_columns
    assert {"AWAITING_ACCEPTANCE", "AWAITING_MERGE", "COMPLETED"} <= set(
        requirement_state.split("'")[1::2]
    )
    assert {"AWAITING_MERGE", "COMPLETED"} <= set(work_item_state.split("'")[1::2])
    assert {
        "NOT_STARTED",
        "MR_PENDING",
        "MR_OPEN",
        "MERGE_PENDING",
        "MERGED",
        "BLOCKED",
        "RECONCILIATION_PENDING",
    } <= set(formal_delivery_state.split("'")[1::2])
    assert formal_gate_index["unique"] is True
    assert formal_gate_index["column_names"] == [
        "formal_merge_request_binding_id",
        "selection_id",
    ]


def test_v06_runtime_grants_keep_delivery_facts_append_only(
    isolated_requirement_rw_engine: Engine,
) -> None:
    with isolated_requirement_rw_engine.connect() as db:
        privileges = {
            table_name: {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
                if db.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'requirement_rw', 'requirement.' || :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
            }
            for table_name in V06_TABLES
        }
        formal_update_columns = {
            column_name: db.execute(
                text(
                    "SELECT has_column_privilege("
                    "'requirement_rw', 'requirement.work_item', :column_name, 'UPDATE')"
                ),
                {"column_name": column_name},
            ).scalar_one()
            for column_name in (
                "formal_delivery_state",
                "formal_merge_request_binding_id",
                "formal_blocked_reason_code",
                "formal_updated_at",
            )
        }
        has_table_update = db.execute(
            text("SELECT has_table_privilege('requirement_rw', 'requirement.work_item', 'UPDATE')")
        ).scalar_one()
        forbidden_update_columns = {
            column_name: db.execute(
                text(
                    "SELECT has_column_privilege("
                    "'requirement_rw', 'requirement.work_item', :column_name, 'UPDATE')"
                ),
                {"column_name": column_name},
            ).scalar_one()
            for column_name in ("created_by", "required_capabilities")
        }
        selection_subject_hash_update = db.execute(
            text(
                "SELECT has_column_privilege("
                "'requirement_rw', 'requirement.integration_baseline_selection', "
                "'delivery_snapshot_hash', 'UPDATE')"
            )
        ).scalar_one()

    assert privileges["requirement_delivery_snapshot"] == {"SELECT", "INSERT"}
    assert privileges["integration_baseline_selection"] == {"SELECT", "INSERT"}
    assert privileges["delivery_gate"] == {"SELECT", "INSERT"}
    assert privileges["delivery_gate_assignment"] == {"SELECT", "INSERT"}
    assert privileges["delivery_decision"] == {"SELECT", "INSERT"}
    assert has_table_update is False
    assert all(formal_update_columns.values())
    assert not any(forbidden_update_columns.values())
    assert selection_subject_hash_update is False


@pytest.mark.parametrize(
    "mismatched_values",
    [
        {"evidence_requirement_version": 8},
        {"evidence_set_version": 4},
        {"evidence_set_hash": "sha256:" + "f" * 64},
        {"snapshot_hash": "sha256:" + "f" * 64},
        {"requirement_version_before": 8, "requirement_version_after": 9},
    ],
    ids=[
        "evidence-requirement-version",
        "work-item-set-version",
        "work-item-set-hash",
        "snapshot-hash",
        "selection-requirement-version",
    ],
)
def test_requirement_rw_cannot_cross_bind_selection_to_a_different_snapshot_subject(
    isolated_requirement_database: IsolatedRequirementDatabase,
    mismatched_values: dict[str, object],
) -> None:
    with isolated_requirement_database.owner.begin() as db:
        _insert_requirement(db)
        _insert_delivery_snapshot(db)

    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(IntegrityError):
            _insert_selection(db, **mismatched_values)


@pytest.mark.parametrize(
    ("mismatched_field", "mismatched_value"),
    [
        ("requirement_version", 9),
        (
            "integration_baseline_id",
            "50000000-0000-0000-0000-000000000602",
        ),
        ("integration_baseline_hash", "sha256:" + "f" * 64),
    ],
)
def test_requirement_rw_cannot_cross_bind_gate_to_a_different_selection_subject(
    isolated_requirement_database: IsolatedRequirementDatabase,
    mismatched_field: str,
    mismatched_value: object,
) -> None:
    with isolated_requirement_database.owner.begin() as db:
        _insert_requirement(db)
        _insert_delivery_snapshot(db)
        _insert_selection(db)

    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(IntegrityError):
            _insert_acceptance_gate(db, **{mismatched_field: mismatched_value})


def test_current_selection_foreign_key_cannot_cross_requirements(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    with isolated_requirement_database.owner.begin() as db:
        _insert_requirement(db)
        db.execute(
            text(
                "INSERT INTO requirement.requirement "
                "(id, workspace_id, type, title, description, acceptance_criteria, "
                "acceptance_criteria_version, acceptance_criteria_hash, created_by, "
                "initial_repository_id, route_snapshot_version, route_snapshot_hash, "
                "route_snapshot, state, record_state, requirement_version, "
                "required_work_item_set_version, required_work_item_set_hash, revision) "
                "SELECT '10000000-0000-0000-0000-000000000602', workspace_id, type, "
                "'Other', description, acceptance_criteria, acceptance_criteria_version, "
                "acceptance_criteria_hash, created_by, initial_repository_id, "
                "route_snapshot_version, route_snapshot_hash, route_snapshot, state, "
                "record_state, requirement_version, required_work_item_set_version, "
                "required_work_item_set_hash, revision "
                "FROM requirement.requirement "
                "WHERE id='10000000-0000-0000-0000-000000000601'"
            )
        )
        _insert_delivery_snapshot(db)
        _insert_selection(db)
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "UPDATE requirement.requirement "
                    "SET current_integration_baseline_selection_id="
                    "'40000000-0000-0000-0000-000000000601' "
                    "WHERE id='10000000-0000-0000-0000-000000000602'"
                )
            )
