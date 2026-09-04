import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_migration import (
    VALID_ARTIFACT_REFERENCES,
    _insert_external_validation,
    _insert_integration_graph,
)

pytestmark = pytest.mark.integration


EVIDENCE_TABLES = {
    "external_validation_receipt",
    "external_validation_reference",
    "evidence_request_inbox",
    "integration_baseline_evidence",
    "integration_baseline_evidence_item",
}


def test_v06_source_control_evidence_tables_and_scoped_runtime_grants_exist(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    inspector = inspect(isolated_source_control_database.owner)
    tables = set(inspector.get_table_names(schema="source_control"))
    with isolated_source_control_database.owner.connect() as db:
        privileges = {
            table_name: {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
                if db.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'source_control_rw', 'source_control.' || :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
            }
            for table_name in EVIDENCE_TABLES
            if table_name in tables
        }

    assert EVIDENCE_TABLES <= tables
    assert privileges["external_validation_receipt"] == {"SELECT", "INSERT"}
    assert privileges["external_validation_reference"] == {"SELECT", "INSERT"}
    assert privileges["integration_baseline_evidence"] == {"SELECT", "INSERT"}
    assert privileges["integration_baseline_evidence_item"] == {"SELECT", "INSERT"}
    assert privileges["evidence_request_inbox"] == {"SELECT", "INSERT"}


def _config(database: IsolatedSourceControlDatabase) -> Config:
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        database.url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


def test_external_validation_receipt_schema_binds_message_to_canonical_fact(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    inspector = inspect(isolated_source_control_database.owner)
    columns = {
        column["name"]: column
        for column in inspector.get_columns(
            "external_validation_receipt",
            schema="source_control",
        )
    }
    primary_key = inspector.get_pk_constraint(
        "external_validation_receipt",
        schema="source_control",
    )
    foreign_keys = {
        item["name"]: item
        for item in inspector.get_foreign_keys(
            "external_validation_receipt",
            schema="source_control",
        )
    }
    checks = {
        item["name"]: item["sqltext"]
        for item in inspector.get_check_constraints(
            "external_validation_receipt",
            schema="source_control",
        )
    }

    assert tuple(primary_key["constrained_columns"]) == ("message_id",)
    assert columns["request_fingerprint"]["nullable"] is False
    assert columns["outcome"]["nullable"] is False
    assert columns["canonical_external_validation_id"]["nullable"] is True
    assert columns["rejection_reason_code"]["nullable"] is True
    fact_fk = foreign_keys["fk_sc_validation_receipt_fact"]
    assert fact_fk["constrained_columns"] == [
        "canonical_external_validation_id",
        "requirement_id",
        "work_item_id",
    ]
    assert fact_fk["referred_columns"] == ["id", "requirement_id", "work_item_id"]
    assert "request_fingerprint" in checks["ck_sc_validation_receipt_fingerprint"]
    outcome_check = checks["ck_sc_validation_receipt_outcome"]
    assert "outcome" in outcome_check
    assert "canonical_external_validation_id" in outcome_check
    assert "rejection_reason_code" in outcome_check


def test_external_validation_receipt_is_runtime_append_only(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    canonical_id = "90000000-0000-0000-0000-000000000681"
    message_id = "93000000-0000-0000-0000-000000000681"
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        _insert_external_validation(
            db,
            reference_id=canonical_id,
            reference="urn:ci:platform:681",
            artifact_references=VALID_ARTIFACT_REFERENCES,
        )
    with isolated_source_control_database.runtime.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.external_validation_receipt "
                "(message_id, request_fingerprint, outcome, "
                "canonical_external_validation_id, "
                "requirement_id, work_item_id) VALUES "
                "(:message_id, :request_fingerprint, 'ACCEPTED', :canonical_id, "
                "'40000000-0000-0000-0000-000000000301', "
                "'50000000-0000-0000-0000-000000000301')"
            ),
            {
                "message_id": message_id,
                "request_fingerprint": "sha256:" + "9" * 64,
                "canonical_id": canonical_id,
            },
        )

    with pytest.raises(ProgrammingError):
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                text(
                    "UPDATE source_control.external_validation_receipt "
                    "SET request_fingerprint=:request_fingerprint "
                    "WHERE message_id=:message_id"
                ),
                {
                    "message_id": message_id,
                    "request_fingerprint": "sha256:" + "8" * 64,
                },
            )
    with pytest.raises(ProgrammingError):
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                text(
                    "DELETE FROM source_control.external_validation_receipt "
                    "WHERE message_id=:message_id"
                ),
                {"message_id": message_id},
            )


@pytest.mark.parametrize(
    ("outcome", "canonical_id", "rejection_reason_code"),
    [
        pytest.param("ACCEPTED", None, None, id="accepted-without-fact"),
        pytest.param(
            "ACCEPTED",
            "90000000-0000-0000-0000-000000000683",
            "EVIDENCE_STALE",
            id="accepted-with-reason",
        ),
        pytest.param(
            "REJECTED",
            "90000000-0000-0000-0000-000000000683",
            "EVIDENCE_STALE",
            id="rejected-with-fact",
        ),
        pytest.param("REJECTED", None, "OTHER", id="rejected-with-unknown-reason"),
    ],
)
def test_external_validation_receipt_outcome_shape_is_database_enforced(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    outcome: str,
    canonical_id: str | None,
    rejection_reason_code: str | None,
) -> None:
    stored_canonical_id = "90000000-0000-0000-0000-000000000683"
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        _insert_external_validation(
            db,
            reference_id=stored_canonical_id,
            reference="urn:ci:platform:683",
            artifact_references=VALID_ARTIFACT_REFERENCES,
        )

    with pytest.raises(IntegrityError):
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                text(
                    "INSERT INTO source_control.external_validation_receipt "
                    "(message_id, request_fingerprint, outcome, "
                    "canonical_external_validation_id, requirement_id, work_item_id, "
                    "rejection_reason_code) VALUES "
                    "('93000000-0000-0000-0000-000000000683', :fingerprint, "
                    ":outcome, :canonical_id, "
                    "'40000000-0000-0000-0000-000000000301', "
                    "'50000000-0000-0000-0000-000000000301', :reason_code)"
                ),
                {
                    "fingerprint": "sha256:" + "6" * 64,
                    "outcome": outcome,
                    "canonical_id": canonical_id,
                    "reason_code": rejection_reason_code,
                },
            )

    with isolated_source_control_database.runtime.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.external_validation_receipt "
                "(message_id, request_fingerprint, outcome, "
                "canonical_external_validation_id, requirement_id, work_item_id, "
                "rejection_reason_code) VALUES "
                "('93000000-0000-0000-0000-000000000684', :fingerprint, "
                "'REJECTED', NULL, "
                "'40000000-0000-0000-0000-000000000301', "
                "'50000000-0000-0000-0000-000000000301', 'EVIDENCE_STALE')"
            ),
            {"fingerprint": "sha256:" + "5" * 64},
        )


def test_external_validation_receipt_blocks_0007_downgrade_before_ddl(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    config = _config(isolated_source_control_database)
    command.downgrade(config, "source_control@0007_sc_evidence")
    message_id = "93000000-0000-0000-0000-000000000682"
    with isolated_source_control_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.external_validation_receipt "
                "(message_id, request_fingerprint, outcome, "
                "requirement_id, work_item_id, rejection_reason_code) VALUES "
                "(:message_id, :request_fingerprint, 'REJECTED', "
                "'40000000-0000-0000-0000-000000000301', "
                "'50000000-0000-0000-0000-000000000301', 'EVIDENCE_STALE')"
            ),
            {
                "message_id": message_id,
                "request_fingerprint": "sha256:" + "7" * 64,
            },
        )

    with pytest.raises(Exception, match="V0.6 Evidence facts"):
        command.downgrade(config, "source_control@0006_sc_mr_reconcile")

    assert inspect(isolated_source_control_database.owner).has_table(
        "external_validation_receipt",
        schema="source_control",
    )
    with isolated_source_control_database.owner.connect() as db:
        preserved = db.execute(
            text(
                "SELECT outcome, rejection_reason_code "
                "FROM source_control.external_validation_receipt "
                "WHERE message_id=:message_id"
            ),
            {"message_id": message_id},
        ).one()
    assert tuple(preserved) == ("REJECTED", "EVIDENCE_STALE")


def test_clean_0007_downgrade_removes_external_validation_receipts(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    command.downgrade(
        _config(isolated_source_control_database),
        "source_control@0006_sc_mr_reconcile",
    )

    inspector = inspect(isolated_source_control_database.owner)
    assert not inspector.has_table(
        "external_validation_receipt",
        schema="source_control",
    )
    assert not inspector.has_table(
        "external_validation_reference",
        schema="source_control",
    )


def test_evidence_inbox_runtime_can_update_state_but_not_envelope_coordinates(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    message_id = "94000000-0000-0000-0000-000000000641"
    with isolated_source_control_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.evidence_request_inbox "
                "(message_id, topic, payload_hash, delivery_snapshot_id, "
                "delivery_snapshot_hash, requirement_id, requirement_version, "
                "required_work_item_set_version, required_work_item_set_hash, "
                "work_item_ids, state) VALUES (:message_id, "
                "'requirement.integration-baseline.requested', :payload_hash, "
                "'92000000-0000-0000-0000-000000000641', :snapshot_hash, "
                "'40000000-0000-0000-0000-000000000301', 7, 3, :set_hash, "
                "'[\"50000000-0000-0000-0000-000000000301\"]'::jsonb, 'RECEIVED')"
            ),
            {
                "message_id": message_id,
                "payload_hash": "sha256:" + "1" * 64,
                "snapshot_hash": "sha256:" + "2" * 64,
                "set_hash": "sha256:" + "3" * 64,
            },
        )
    with isolated_source_control_database.runtime.begin() as db:
        db.execute(
            text(
                "UPDATE source_control.evidence_request_inbox "
                "SET state='FAILED', last_error_code='EVIDENCE_STALE', updated_at=now() "
                "WHERE message_id=:message_id"
            ),
            {"message_id": message_id},
        )

    with pytest.raises(ProgrammingError):
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                text(
                    "UPDATE source_control.evidence_request_inbox "
                    "SET payload_hash=:payload_hash WHERE message_id=:message_id"
                ),
                {
                    "message_id": message_id,
                    "payload_hash": "sha256:" + "4" * 64,
                },
            )


def test_external_validation_persists_a_required_request_fingerprint(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    inspector = inspect(isolated_source_control_database.owner)
    columns = {
        column["name"]: column
        for column in inspector.get_columns(
            "external_validation_reference",
            schema="source_control",
        )
    }
    checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints(
            "external_validation_reference",
            schema="source_control",
        )
    }

    assert columns["request_fingerprint"]["nullable"] is False
    assert "request_fingerprint" in checks["ck_sc_validation_hash"]


def test_external_validation_must_match_the_exact_integration_binding(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_observation "
                "(id, binding_id, head_sha, state, merge_commit_sha, "
                "external_merge_user_id, merged_at, observation_digest, observed_at) VALUES "
                "('80000000-0000-0000-0000-000000000302', "
                "'71000000-0000-0000-0000-000000000301', :head_sha, 'MERGED', "
                ":merge_sha, '42', now(), 'sha256:merged', now())"
            ),
            {"head_sha": "b" * 40, "merge_sha": "c" * 40},
        )
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO source_control.external_validation_reference "
                    "(id, work_item_id, requirement_id, workspace_id, "
                    "integration_merge_request_binding_id, target_commit_sha, "
                    "integration_merge_commit_sha, reference, notes, artifact_references, "
                    "reference_hash, request_fingerprint, submitted_by) VALUES "
                    "('90000000-0000-0000-0000-000000000601', "
                    "'50000000-0000-0000-0000-000000000399', "
                    "'40000000-0000-0000-0000-000000000301', "
                    "'20000000-0000-0000-0000-000000000301', "
                    "'71000000-0000-0000-0000-000000000301', :head_sha, :merge_sha, "
                    "'https://jenkins.example.test/job/platform/42', 'verified', "
                    "CAST(:artifact_references AS JSONB), "
                    ":reference_hash, :request_fingerprint, 'employee-1')"
                ),
                {
                    "head_sha": "b" * 40,
                    "merge_sha": "c" * 40,
                    "artifact_references": VALID_ARTIFACT_REFERENCES,
                    "reference_hash": "sha256:" + "d" * 64,
                    "request_fingerprint": "sha256:" + "e" * 64,
                },
            )


def test_evidence_item_must_reference_the_same_requirement_and_validation(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        db.execute(
            text(
                "INSERT INTO source_control.external_validation_reference "
                "(id, work_item_id, requirement_id, workspace_id, "
                "integration_merge_request_binding_id, target_commit_sha, "
                "integration_merge_commit_sha, reference, notes, artifact_references, "
                "reference_hash, request_fingerprint, submitted_by) VALUES "
                "('90000000-0000-0000-0000-000000000601', "
                "'50000000-0000-0000-0000-000000000301', "
                "'40000000-0000-0000-0000-000000000301', "
                "'20000000-0000-0000-0000-000000000301', "
                "'71000000-0000-0000-0000-000000000301', :head_sha, :merge_sha, "
                "'https://jenkins.example.test/job/platform/42', 'verified', "
                "CAST(:artifact_references AS JSONB), "
                ":reference_hash, :request_fingerprint, 'employee-1')"
            ),
            {
                "head_sha": "b" * 40,
                "merge_sha": "c" * 40,
                "artifact_references": VALID_ARTIFACT_REFERENCES,
                "reference_hash": "sha256:" + "d" * 64,
                "request_fingerprint": "sha256:" + "e" * 64,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.integration_baseline_evidence "
                "(id, delivery_snapshot_id, delivery_snapshot_hash, requirement_id, "
                "requirement_version, required_work_item_set_version, "
                "required_work_item_set_hash, evidence_hash, generated_by) VALUES "
                "('91000000-0000-0000-0000-000000000601', "
                "'92000000-0000-0000-0000-000000000601', :snapshot_hash, "
                "'40000000-0000-0000-0000-000000000301', 7, 3, :set_hash, "
                ":evidence_hash, 'SYSTEM')"
            ),
            {
                "snapshot_hash": "sha256:" + "e" * 64,
                "set_hash": "sha256:" + "f" * 64,
                "evidence_hash": "sha256:" + "1" * 64,
            },
        )
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO source_control.integration_baseline_evidence_item "
                    "(evidence_id, requirement_id, work_item_id, repository_id, task_branch, "
                    "task_commit_sha, integration_merge_request_binding_id, "
                    "integration_merge_request_iid, integration_merge_commit_sha, "
                    "executor_type, executor_id, artifact_references, "
                    "external_validation_reference_id, item_hash) VALUES "
                    "('91000000-0000-0000-0000-000000000601', "
                    "'40000000-0000-0000-0000-000000000399', "
                    "'50000000-0000-0000-0000-000000000301', "
                    "'10000000-0000-0000-0000-000000000301', "
                    "'feat/wi-301-source-control', :head_sha, "
                    "'71000000-0000-0000-0000-000000000301', 42, :merge_sha, "
                    "'HUMAN', 'employee-1', CAST(:artifact_references AS JSONB), "
                    "'90000000-0000-0000-0000-000000000601', :item_hash)"
                ),
                {
                    "head_sha": "b" * 40,
                    "merge_sha": "c" * 40,
                    "artifact_references": VALID_ARTIFACT_REFERENCES,
                    "item_hash": "sha256:" + "2" * 64,
                },
            )
