import json

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_migration import _insert_integration_graph

pytestmark = pytest.mark.integration


FORMAL_TABLES = {
    "formal_delivery_request_inbox",
    "formal_review_assignment",
}

WORK_ITEM_ID = "50000000-0000-0000-0000-000000000301"
REQUIREMENT_ID = "40000000-0000-0000-0000-000000000301"
REPOSITORY_ID = "10000000-0000-0000-0000-000000000301"
BRANCH_BINDING_ID = "70000000-0000-0000-0000-000000000301"
FORMAL_BINDING_ID = "98000000-0000-0000-0000-000000000601"
ACCEPTANCE_DECISION_ID = "99000000-0000-0000-0000-000000000601"
REVIEW_DECISION_ID = "99000000-0000-0000-0000-000000000602"
HEAD_SHA = "b" * 40
REQUEST_FINGERPRINT = "sha256:" + "6" * 64


def _insert_formal_effect(
    db: Connection,
    *,
    operation: str,
    subject_key: str,
    payload: dict[str, object],
) -> None:
    db.execute(
        text(
            "INSERT INTO source_control.source_control_effect "
            "(id, effect_key, operation, subject_key, payload, work_item_id, "
            "requirement_id, repository_id, request_fingerprint, attempts, state, "
            "requirement_callback_state) VALUES "
            "('60000000-0000-0000-0000-000000000606', 'formal-effect-606', "
            ":operation, :subject_key, CAST(:payload AS JSONB), :work_item_id, "
            ":requirement_id, :repository_id, :request_fingerprint, 1, 'IN_FLIGHT', "
            "'PENDING')"
        ),
        {
            "operation": operation,
            "subject_key": subject_key,
            "payload": json.dumps(payload, separators=(",", ":")),
            "work_item_id": WORK_ITEM_ID,
            "requirement_id": REQUIREMENT_ID,
            "repository_id": REPOSITORY_ID,
            "request_fingerprint": REQUEST_FINGERPRINT,
        },
    )


def _insert_formal_inbox(
    db: Connection,
    *,
    state: str,
    processed_at: str | None,
) -> None:
    db.execute(
        text(
            "INSERT INTO source_control.formal_delivery_request_inbox "
            "(message_id, topic, payload_hash, requirement_id, requirement_revision, "
            "work_item_id, work_item_revision, repository_id, actor_id, "
            "acceptance_decision_id, formal_merge_request_binding_id, "
            "formal_review_decision_id, requested_head_sha, state, processed_at) VALUES "
            "('61000000-0000-0000-0000-000000000606', "
            "'requirement.formal-merge-request.requested', :payload_hash, "
            ":requirement_id, 7, :work_item_id, 3, :repository_id, 'employee-1', "
            ":acceptance_decision_id, NULL, NULL, :head_sha, :state, :processed_at)"
        ),
        {
            "payload_hash": "sha256:" + "7" * 64,
            "requirement_id": REQUIREMENT_ID,
            "work_item_id": WORK_ITEM_ID,
            "repository_id": REPOSITORY_ID,
            "acceptance_decision_id": ACCEPTANCE_DECISION_ID,
            "head_sha": HEAD_SHA,
            "state": state,
            "processed_at": processed_at,
        },
    )


def _insert_integration_create_effect(
    db: Connection,
    *,
    effect_id: str,
    head_sha: str,
) -> None:
    db.execute(
        text(
            "INSERT INTO source_control.source_control_effect "
            "(id, effect_key, operation, subject_key, payload, work_item_id, "
            "requirement_id, repository_id, request_fingerprint, attempts, state, "
            "requirement_callback_state, completed_at) VALUES "
            "(:effect_id, :effect_key, 'CREATE_INTEGRATION_MR', :subject_key, "
            "CAST(:payload AS JSONB), :work_item_id, :requirement_id, :repository_id, "
            "'sha256:create-integration-mr-mismatch', 1, 'SUCCEEDED', 'PENDING', now())"
        ),
        {
            "effect_id": effect_id,
            "effect_key": (f"source-control:create-integration-mr:{WORK_ITEM_ID}:{head_sha}"),
            "subject_key": f"integration-work-item:{WORK_ITEM_ID}:{head_sha}",
            "payload": json.dumps(
                {"branchBindingId": BRANCH_BINDING_ID, "headSha": head_sha},
                separators=(",", ":"),
            ),
            "work_item_id": WORK_ITEM_ID,
            "requirement_id": REQUIREMENT_ID,
            "repository_id": REPOSITORY_ID,
        },
    )


def _insert_merge_request_binding(
    db: Connection,
    *,
    binding_id: str,
    kind: str,
    target_branch: str,
    create_effect_id: str,
    head_sha: str,
    merge_request_iid: int,
) -> None:
    db.execute(
        text(
            "INSERT INTO source_control.merge_request_binding "
            "(id, kind, work_item_id, requirement_id, workspace_id, repository_id, "
            "branch_binding_id, external_project_id, merge_request_iid, "
            "source_branch, target_branch, create_effect_id, head_sha, "
            "creation_origin) VALUES "
            "(:binding_id, :kind, :work_item_id, :requirement_id, "
            "'20000000-0000-0000-0000-000000000301', :repository_id, "
            ":branch_binding_id, '101', :merge_request_iid, "
            "'feat/wi-301-source-control', :target_branch, :create_effect_id, "
            ":head_sha, 'PLATFORM_CREATED')"
        ),
        {
            "binding_id": binding_id,
            "kind": kind,
            "work_item_id": WORK_ITEM_ID,
            "requirement_id": REQUIREMENT_ID,
            "repository_id": REPOSITORY_ID,
            "branch_binding_id": BRANCH_BINDING_ID,
            "merge_request_iid": merge_request_iid,
            "target_branch": target_branch,
            "create_effect_id": create_effect_id,
            "head_sha": head_sha,
        },
    )


def test_v06_formal_tables_and_runtime_grants_exist(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    owner = isolated_source_control_database.owner
    inspector = inspect(owner)
    tables = set(inspector.get_table_names(schema="source_control"))
    assignment_columns = {
        column["name"]: column
        for column in inspector.get_columns(
            "formal_review_assignment",
            schema="source_control",
        )
    }
    assignment_uniques = {
        constraint["name"]: constraint
        for constraint in inspector.get_unique_constraints(
            "formal_review_assignment",
            schema="source_control",
        )
    }
    with owner.connect() as db:
        inbox_privileges = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            if db.execute(
                text(
                    "SELECT has_table_privilege("
                    "'source_control_rw', "
                    "'source_control.formal_delivery_request_inbox', :privilege)"
                ),
                {"privilege": privilege},
            ).scalar_one()
        }
        review_privileges = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            if db.execute(
                text(
                    "SELECT has_table_privilege("
                    "'source_control_rw', "
                    "'source_control.formal_review_assignment', :privilege)"
                ),
                {"privilege": privilege},
            ).scalar_one()
        }
        can_supersede = db.execute(
            text(
                "SELECT has_column_privilege("
                "'source_control_rw', "
                "'source_control.formal_review_assignment', "
                "'superseded_at', 'UPDATE')"
            )
        ).scalar_one()
        can_rewrite_snapshot = db.execute(
            text(
                "SELECT has_column_privilege("
                "'source_control_rw', "
                "'source_control.formal_review_assignment', "
                "'resolution_snapshot', 'UPDATE')"
            )
        ).scalar_one()

    assert FORMAL_TABLES <= tables
    assert assignment_columns["acceptance_decision_id"]["nullable"] is False
    assert tuple(assignment_uniques["uq_sc_formal_review_acceptance"]["column_names"]) == (
        "binding_id",
        "acceptance_decision_id",
    )
    assert inbox_privileges == {"SELECT", "INSERT"}
    assert review_privileges == {"SELECT", "INSERT"}
    assert can_supersede is True
    assert can_rewrite_snapshot is False


def test_integration_binding_history_has_current_indexes_and_column_scoped_acl(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    owner = isolated_source_control_database.owner
    inspector = inspect(owner)
    columns = {
        column["name"]: column
        for column in inspector.get_columns(
            "merge_request_binding",
            schema="source_control",
        )
    }
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes(
            "merge_request_binding",
            schema="source_control",
        )
    }
    with owner.connect() as db:
        can_supersede = db.execute(
            text(
                "SELECT has_column_privilege("
                "'source_control_rw', "
                "'source_control.merge_request_binding', "
                "'superseded_at', 'UPDATE')"
            )
        ).scalar_one()
        can_rewrite_head = db.execute(
            text(
                "SELECT has_column_privilege("
                "'source_control_rw', "
                "'source_control.merge_request_binding', "
                "'head_sha', 'UPDATE')"
            )
        ).scalar_one()

    assert columns["superseded_at"]["nullable"] is True
    assert indexes["uq_sc_current_mr_binding_kind_work_item"]["unique"] is True
    assert indexes["uq_sc_current_mr_binding_kind_branch"]["unique"] is True
    assert tuple(indexes["ix_sc_mr_binding_kind_work_item_history"]["column_names"]) == (
        "kind",
        "work_item_id",
        "created_at",
        "id",
    )
    assert can_supersede is True
    assert can_rewrite_head is False


def test_merge_request_binding_create_effect_fk_is_discriminated_by_operation(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    inspector = inspect(isolated_source_control_database.owner)
    binding_columns = {
        column["name"]: column
        for column in inspector.get_columns(
            "merge_request_binding",
            schema="source_control",
        )
    }
    binding_foreign_keys = {
        item["name"]: item
        for item in inspector.get_foreign_keys(
            "merge_request_binding",
            schema="source_control",
        )
    }
    effect_uniques = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "source_control_effect",
            schema="source_control",
        )
    }

    assert "create_effect_operation" in binding_columns
    operation_column = binding_columns["create_effect_operation"]
    assert operation_column["nullable"] is False
    assert operation_column["computed"]["persisted"] is True
    generation_expression = operation_column["computed"]["sqltext"]
    assert "CREATE_INTEGRATION_MR" in generation_expression
    assert "CREATE_FORMAL_MR" in generation_expression

    create_effect_fk = binding_foreign_keys["fk_source_control_mr_binding_effect"]
    assert create_effect_fk["constrained_columns"] == [
        "create_effect_id",
        "create_effect_operation",
    ]
    assert create_effect_fk["referred_columns"] == ["id", "operation"]
    assert effect_uniques["uq_sc_effect_id_operation"] == ("id", "operation")


def test_formal_binding_rejects_integration_create_effect(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    integration_effect_id = "60000000-0000-0000-0000-000000000307"
    integration_head_sha = "d" * 40
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        _insert_integration_create_effect(
            db,
            effect_id=integration_effect_id,
            head_sha=integration_head_sha,
        )

        with pytest.raises(IntegrityError):
            _insert_merge_request_binding(
                db,
                binding_id="71000000-0000-0000-0000-000000000307",
                kind="FORMAL",
                target_branch="main",
                create_effect_id=integration_effect_id,
                head_sha=integration_head_sha,
                merge_request_iid=44,
            )


def test_integration_binding_rejects_formal_create_effect(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    formal_effect_id = "60000000-0000-0000-0000-000000000606"
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        db.execute(
            text(
                "UPDATE source_control.merge_request_binding SET superseded_at=now() "
                "WHERE id='71000000-0000-0000-0000-000000000301'"
            )
        )
        _insert_formal_effect(
            db,
            operation="CREATE_FORMAL_MR",
            subject_key=(f"formal-work-item:{WORK_ITEM_ID}:{HEAD_SHA}:{REQUEST_FINGERPRINT}"),
            payload={
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "branchBindingId": BRANCH_BINDING_ID,
                "headSha": HEAD_SHA,
            },
        )

        with pytest.raises(IntegrityError):
            _insert_merge_request_binding(
                db,
                binding_id="71000000-0000-0000-0000-000000000306",
                kind="INTEGRATION",
                target_branch="dev",
                create_effect_id=formal_effect_id,
                head_sha=HEAD_SHA,
                merge_request_iid=44,
            )


def test_delivery_facts_bind_to_their_exact_merge_request_kind(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    inspector = inspect(isolated_source_control_database.owner)
    expected_foreign_keys = {
        ("delivery_request_inbox", "fk_source_control_delivery_mr_binding"): (
            [
                "integration_merge_request_binding_id",
                "work_item_id",
                "requirement_id",
                "integration_binding_kind",
            ],
            ["id", "work_item_id", "requirement_id", "kind"],
        ),
        ("external_validation_reference", "fk_sc_validation_binding"): (
            [
                "integration_merge_request_binding_id",
                "work_item_id",
                "requirement_id",
                "workspace_id",
                "integration_binding_kind",
            ],
            ["id", "work_item_id", "requirement_id", "workspace_id", "kind"],
        ),
        ("integration_baseline_evidence_item", "fk_sc_evidence_item_binding"): (
            [
                "integration_merge_request_binding_id",
                "work_item_id",
                "requirement_id",
                "integration_binding_kind",
            ],
            ["id", "work_item_id", "requirement_id", "kind"],
        ),
        ("formal_delivery_request_inbox", "fk_sc_formal_inbox_binding"): (
            [
                "formal_merge_request_binding_id",
                "work_item_id",
                "requirement_id",
                "formal_binding_kind",
            ],
            ["id", "work_item_id", "requirement_id", "kind"],
        ),
        ("formal_review_assignment", "fk_sc_formal_review_binding"): (
            ["binding_id", "work_item_id", "requirement_id", "binding_kind"],
            ["id", "work_item_id", "requirement_id", "kind"],
        ),
    }

    for (table_name, constraint_name), expected in expected_foreign_keys.items():
        foreign_keys = {
            item["name"]: item
            for item in inspector.get_foreign_keys(table_name, schema="source_control")
        }
        foreign_key = foreign_keys[constraint_name]
        assert foreign_key["constrained_columns"] == expected[0]
        assert foreign_key["referred_columns"] == expected[1]

    expected_kind_checks = {
        ("delivery_request_inbox", "ck_sc_delivery_inbox_binding_kind"): "INTEGRATION",
        ("external_validation_reference", "ck_sc_validation_binding_kind"): "INTEGRATION",
        ("integration_baseline_evidence_item", "ck_sc_evidence_item_binding_kind"): ("INTEGRATION"),
        ("formal_delivery_request_inbox", "ck_sc_formal_inbox_binding_kind"): "FORMAL",
        ("formal_review_assignment", "ck_sc_formal_review_values"): "FORMAL",
    }
    for (table_name, constraint_name), binding_kind in expected_kind_checks.items():
        checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints(table_name, schema="source_control")
        }
        assert f"'{binding_kind}'" in checks[constraint_name]


def test_runtime_can_append_a_new_current_integration_binding_but_not_rewrite_history(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    second_effect_id = "60000000-0000-0000-0000-000000000307"
    second_binding_id = "71000000-0000-0000-0000-000000000307"
    second_head = "d" * 40
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
    with isolated_source_control_database.runtime.begin() as db:
        db.execute(
            text(
                "UPDATE source_control.merge_request_binding SET superseded_at=now() "
                "WHERE id='71000000-0000-0000-0000-000000000301'"
            )
        )
        db.execute(
            text(
                "INSERT INTO source_control.source_control_effect "
                "(id, effect_key, operation, subject_key, payload, work_item_id, "
                "requirement_id, repository_id, request_fingerprint, attempts, state, "
                "requirement_callback_state, completed_at) VALUES "
                "(:effect_id, :effect_key, 'CREATE_INTEGRATION_MR', :subject_key, "
                "CAST(:payload AS JSONB), :work_item_id, :requirement_id, :repository_id, "
                "'sha256:create-mr-second', 1, 'SUCCEEDED', 'PENDING', now())"
            ),
            {
                "effect_id": second_effect_id,
                "effect_key": f"source-control:create-integration-mr:{WORK_ITEM_ID}:{second_head}",
                "subject_key": f"integration-work-item:{WORK_ITEM_ID}:{second_head}",
                "payload": json.dumps(
                    {"branchBindingId": BRANCH_BINDING_ID, "headSha": second_head},
                    separators=(",", ":"),
                ),
                "work_item_id": WORK_ITEM_ID,
                "requirement_id": REQUIREMENT_ID,
                "repository_id": REPOSITORY_ID,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_binding "
                "(id, kind, work_item_id, requirement_id, workspace_id, repository_id, "
                "branch_binding_id, external_project_id, merge_request_iid, "
                "source_branch, target_branch, create_effect_id, head_sha, "
                "creation_origin) VALUES "
                "(:binding_id, 'INTEGRATION', :work_item_id, :requirement_id, "
                "'20000000-0000-0000-0000-000000000301', :repository_id, "
                ":branch_binding_id, '101', 44, 'feat/wi-301-source-control', 'dev', "
                ":effect_id, :head_sha, 'PLATFORM_CREATED')"
            ),
            {
                "binding_id": second_binding_id,
                "work_item_id": WORK_ITEM_ID,
                "requirement_id": REQUIREMENT_ID,
                "repository_id": REPOSITORY_ID,
                "branch_binding_id": BRANCH_BINDING_ID,
                "effect_id": second_effect_id,
                "head_sha": second_head,
            },
        )

    with pytest.raises(IntegrityError):
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                text(
                    "UPDATE source_control.merge_request_binding SET superseded_at=NULL "
                    "WHERE id='71000000-0000-0000-0000-000000000301'"
                )
            )
    with pytest.raises(ProgrammingError):
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                text(
                    "UPDATE source_control.merge_request_binding SET head_sha=:head_sha "
                    "WHERE id='71000000-0000-0000-0000-000000000301'"
                ),
                {"head_sha": "e" * 40},
            )

    with isolated_source_control_database.owner.connect() as db:
        rows = (
            db.execute(
                text(
                    "SELECT id, head_sha, superseded_at FROM "
                    "source_control.merge_request_binding WHERE work_item_id=:work_item_id "
                    "AND kind='INTEGRATION' ORDER BY created_at, id"
                ),
                {"work_item_id": WORK_ITEM_ID},
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    assert rows[0]["superseded_at"] is not None
    assert rows[0]["head_sha"] == HEAD_SHA
    assert str(rows[1]["id"]) == second_binding_id
    assert rows[1]["head_sha"] == second_head
    assert rows[1]["superseded_at"] is None


def test_formal_inbox_runtime_can_update_state_but_not_envelope_coordinates(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        _insert_formal_inbox(db, state="RECEIVED", processed_at=None)
    with isolated_source_control_database.runtime.begin() as db:
        db.execute(
            text(
                "UPDATE source_control.formal_delivery_request_inbox "
                "SET state='FAILED', last_error_code='FORMAL_DELIVERY_CONFLICT', "
                "updated_at=now() WHERE message_id="
                "'61000000-0000-0000-0000-000000000606'"
            )
        )

    with pytest.raises(ProgrammingError):
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                text(
                    "UPDATE source_control.formal_delivery_request_inbox "
                    "SET requested_head_sha=:head_sha WHERE message_id="
                    "'61000000-0000-0000-0000-000000000606'"
                ),
                {"head_sha": "d" * 40},
            )


def test_formal_and_integration_bindings_can_coexist_for_one_work_item(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        db.execute(
            text(
                "INSERT INTO source_control.source_control_effect "
                "(id, effect_key, operation, subject_key, payload, work_item_id, "
                "requirement_id, repository_id, request_fingerprint, attempts, state, "
                "requirement_callback_state, completed_at) VALUES "
                "('60000000-0000-0000-0000-000000000306', "
                "'create-formal:work-item-301', 'CREATE_FORMAL_MR', "
                "'formal-work-item:50000000-0000-0000-0000-000000000301:"
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:sha256:create-formal', "
                '\'{"acceptanceDecisionId":"90000000-0000-0000-0000-000000000306",'
                '"branchBindingId":"70000000-0000-0000-0000-000000000301",'
                '"headSha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}\'::jsonb, '
                "'50000000-0000-0000-0000-000000000301', "
                "'40000000-0000-0000-0000-000000000301', "
                "'10000000-0000-0000-0000-000000000301', "
                "'sha256:create-formal', 1, 'SUCCEEDED', 'PENDING', now())"
            )
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_binding "
                "(id, kind, work_item_id, requirement_id, workspace_id, repository_id, "
                "branch_binding_id, external_project_id, merge_request_iid, "
                "source_branch, target_branch, create_effect_id, head_sha, "
                "creation_origin) VALUES "
                "('71000000-0000-0000-0000-000000000306', 'FORMAL', "
                "'50000000-0000-0000-0000-000000000301', "
                "'40000000-0000-0000-0000-000000000301', "
                "'20000000-0000-0000-0000-000000000301', "
                "'10000000-0000-0000-0000-000000000301', "
                "'70000000-0000-0000-0000-000000000301', '101', 43, "
                "'feat/wi-301-source-control', 'main', "
                "'60000000-0000-0000-0000-000000000306', "
                "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                "'PLATFORM_CREATED')"
            )
        )
        bindings = db.execute(
            text(
                "SELECT kind, target_branch FROM source_control.merge_request_binding "
                "WHERE work_item_id='50000000-0000-0000-0000-000000000301' "
                "ORDER BY kind"
            )
        ).all()

    assert [tuple(row) for row in bindings] == [
        ("FORMAL", "main"),
        ("INTEGRATION", "dev"),
    ]


def test_formal_binding_rejects_non_main_target(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "UPDATE source_control.merge_request_binding "
                    "SET kind='FORMAL' "
                    "WHERE id='71000000-0000-0000-0000-000000000301'"
                )
            )


@pytest.mark.parametrize(
    ("operation", "subject_key", "payload"),
    [
        pytest.param(
            "CREATE_FORMAL_MR",
            f"formal-work-item:{WORK_ITEM_ID}:{'1' * 40}:{REQUEST_FINGERPRINT}",
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "branchBindingId": BRANCH_BINDING_ID,
                "headSha": int("1" * 40),
            },
            id="create-head-number",
        ),
        pytest.param(
            "MERGE_FORMAL_MR",
            f"formal-mr:{FORMAL_BINDING_ID}:{'1' * 40}:{REQUEST_FINGERPRINT}",
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "bindingId": FORMAL_BINDING_ID,
                "requestedHeadSha": int("1" * 40),
                "reviewDecisionId": REVIEW_DECISION_ID,
            },
            id="merge-head-number",
        ),
    ],
)
def test_formal_effect_head_must_be_a_json_string(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    operation: str,
    subject_key: str,
    payload: dict[str, object],
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        with pytest.raises(IntegrityError):
            _insert_formal_effect(
                db,
                operation=operation,
                subject_key=subject_key,
                payload=payload,
            )


def test_merge_formal_effect_accepts_exact_acceptance_fixed_payload(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    payload: dict[str, object] = {
        "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
        "bindingId": FORMAL_BINDING_ID,
        "requestedHeadSha": HEAD_SHA,
        "reviewDecisionId": REVIEW_DECISION_ID,
    }
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        _insert_formal_effect(
            db,
            operation="MERGE_FORMAL_MR",
            subject_key=f"formal-mr:{FORMAL_BINDING_ID}:{HEAD_SHA}:{REQUEST_FINGERPRINT}",
            payload=payload,
        )
        stored_payload = db.execute(
            text(
                "SELECT payload FROM source_control.source_control_effect "
                "WHERE id='60000000-0000-0000-0000-000000000606'"
            )
        ).scalar_one()

    assert stored_payload == payload


@pytest.mark.parametrize(
    ("operation", "subject_key", "payload"),
    [
        pytest.param(
            "CREATE_FORMAL_MR",
            f"formal-work-item:{WORK_ITEM_ID}:{HEAD_SHA}",
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "branchBindingId": BRANCH_BINDING_ID,
                "headSha": HEAD_SHA,
            },
            id="create-missing-request-fingerprint",
        ),
        pytest.param(
            "MERGE_FORMAL_MR",
            f"formal-mr:{FORMAL_BINDING_ID}:{HEAD_SHA}:sha256:{'5' * 64}",
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "bindingId": FORMAL_BINDING_ID,
                "requestedHeadSha": HEAD_SHA,
                "reviewDecisionId": REVIEW_DECISION_ID,
            },
            id="merge-wrong-request-fingerprint",
        ),
    ],
)
def test_formal_effect_subject_is_bound_to_request_fingerprint(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    operation: str,
    subject_key: str,
    payload: dict[str, object],
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        with pytest.raises(IntegrityError):
            _insert_formal_effect(
                db,
                operation=operation,
                subject_key=subject_key,
                payload=payload,
            )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        pytest.param("acceptanceDecisionId", 601, id="acceptance-decision-number"),
        pytest.param("acceptanceDecisionId", " ", id="acceptance-decision-blank"),
        pytest.param("bindingId", 601, id="binding-number"),
        pytest.param("bindingId", " ", id="binding-blank"),
        pytest.param("requestedHeadSha", int("1" * 40), id="head-number"),
        pytest.param("requestedHeadSha", " ", id="head-blank"),
        pytest.param("reviewDecisionId", 602, id="review-decision-number"),
        pytest.param("reviewDecisionId", " ", id="review-decision-blank"),
    ],
)
def test_merge_formal_effect_fields_are_non_empty_json_strings(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    field_name: str,
    invalid_value: object,
) -> None:
    payload: dict[str, object] = {
        "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
        "bindingId": FORMAL_BINDING_ID,
        "requestedHeadSha": HEAD_SHA,
        "reviewDecisionId": REVIEW_DECISION_ID,
    }
    payload[field_name] = invalid_value
    subject_key = (
        f"formal-mr:{payload['bindingId']}:{payload['requestedHeadSha']}:{REQUEST_FINGERPRINT}"
    )

    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        with pytest.raises(IntegrityError):
            _insert_formal_effect(
                db,
                operation="MERGE_FORMAL_MR",
                subject_key=subject_key,
                payload=payload,
            )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "bindingId": FORMAL_BINDING_ID,
                "requestedHeadSha": HEAD_SHA,
                "reviewDecisionId": REVIEW_DECISION_ID,
            },
            id="missing-acceptance-decision",
        ),
        pytest.param(
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "bindingId": FORMAL_BINDING_ID,
                "requestedHeadSha": HEAD_SHA,
                "reviewDecisionId": REVIEW_DECISION_ID,
                "unexpected": "value",
            },
            id="extra-field",
        ),
    ],
)
def test_merge_formal_effect_rejects_missing_or_extra_payload_fields(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    payload: dict[str, object],
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        with pytest.raises(IntegrityError):
            _insert_formal_effect(
                db,
                operation="MERGE_FORMAL_MR",
                subject_key=(f"formal-mr:{FORMAL_BINDING_ID}:{HEAD_SHA}:{REQUEST_FINGERPRINT}"),
                payload=payload,
            )


@pytest.mark.parametrize(
    ("state", "processed_at"),
    [
        pytest.param("UNKNOWN", None, id="unknown-state"),
        pytest.param("PROCESSED", None, id="processed-without-completion-time"),
        pytest.param(
            "RECEIVED",
            "2026-08-31T08:00:00Z",
            id="received-with-completion-time",
        ),
        pytest.param(
            "PROCESSING",
            "2026-08-31T08:00:00Z",
            id="processing-with-completion-time",
        ),
        pytest.param(
            "FAILED",
            "2026-08-31T08:00:00Z",
            id="failed-with-completion-time",
        ),
    ],
)
def test_formal_inbox_rejects_invalid_state_or_completion_shape(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    state: str,
    processed_at: str | None,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        with pytest.raises(IntegrityError):
            _insert_formal_inbox(db, state=state, processed_at=processed_at)


def test_formal_facts_block_downgrade_before_any_ddl(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        _insert_formal_inbox(db, state="RECEIVED", processed_at=None)

    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        isolated_source_control_database.url.render_as_string(hide_password=False).replace(
            "%", "%%"
        ),
    )
    with pytest.raises(Exception, match="formal delivery facts"):
        command.downgrade(config, "source_control@0007_sc_evidence")

    inspector = inspect(isolated_source_control_database.owner)
    assert inspector.has_table(
        "formal_delivery_request_inbox",
        schema="source_control",
    )
    with isolated_source_control_database.owner.connect() as db:
        preserved = db.execute(
            text(
                "SELECT count(*) FROM source_control.formal_delivery_request_inbox "
                "WHERE message_id='61000000-0000-0000-0000-000000000606'"
            )
        ).scalar_one()
        operation_constraint_exists = db.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conname='ck_source_control_effect_operation_shape')"
            )
        ).scalar_one()

    assert preserved == 1
    assert operation_constraint_exists is True


def test_superseded_integration_binding_blocks_v06_downgrade_before_ddl(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        db.execute(
            text(
                "UPDATE source_control.merge_request_binding SET superseded_at=now() "
                "WHERE id='71000000-0000-0000-0000-000000000301'"
            )
        )

    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        isolated_source_control_database.url.render_as_string(hide_password=False).replace(
            "%", "%%"
        ),
    )
    with pytest.raises(Exception, match="formal delivery facts"):
        command.downgrade(config, "source_control@0007_sc_evidence")

    inspector = inspect(isolated_source_control_database.owner)
    columns = {
        column["name"]
        for column in inspector.get_columns(
            "merge_request_binding",
            schema="source_control",
        )
    }
    assert "superseded_at" in columns


def test_multiple_integration_create_effects_block_v06_downgrade_before_rekey(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    second_effect_id = "60000000-0000-0000-0000-000000000308"
    with isolated_source_control_database.owner.begin() as db:
        _insert_integration_graph(db)
        _insert_integration_create_effect(
            db,
            effect_id=second_effect_id,
            head_sha="e" * 40,
        )

    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        isolated_source_control_database.url.render_as_string(hide_password=False).replace(
            "%", "%%"
        ),
    )
    with pytest.raises(Exception, match="formal delivery facts"):
        command.downgrade(config, "source_control@0007_sc_evidence")

    inspector = inspect(isolated_source_control_database.owner)
    columns = {
        column["name"]
        for column in inspector.get_columns(
            "merge_request_binding",
            schema="source_control",
        )
    }
    with isolated_source_control_database.owner.connect() as db:
        preserved = db.execute(
            text(
                "SELECT count(*) FROM source_control.source_control_effect "
                "WHERE work_item_id=:work_item_id "
                "AND operation='CREATE_INTEGRATION_MR'"
            ),
            {"work_item_id": WORK_ITEM_ID},
        ).scalar_one()
    assert "create_effect_operation" in columns
    assert preserved == 2


def test_v06_downgrade_restores_single_column_create_effect_fk(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        isolated_source_control_database.url.render_as_string(hide_password=False).replace(
            "%", "%%"
        ),
    )

    command.downgrade(config, "source_control@0007_sc_evidence")

    inspector = inspect(isolated_source_control_database.owner)
    binding_columns = {
        column["name"]
        for column in inspector.get_columns(
            "merge_request_binding",
            schema="source_control",
        )
    }
    binding_foreign_keys = {
        item["name"]: item
        for item in inspector.get_foreign_keys(
            "merge_request_binding",
            schema="source_control",
        )
    }
    binding_checks = {
        item["name"]: item["sqltext"]
        for item in inspector.get_check_constraints(
            "merge_request_binding",
            schema="source_control",
        )
    }
    effect_uniques = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "source_control_effect",
            schema="source_control",
        )
    }

    assert "create_effect_operation" not in binding_columns
    create_effect_fk = binding_foreign_keys["fk_source_control_mr_binding_effect"]
    assert create_effect_fk["constrained_columns"] == ["create_effect_id"]
    assert create_effect_fk["referred_columns"] == ["id"]
    assert "uq_sc_effect_id_operation" not in effect_uniques
    assert "INTEGRATION" in binding_checks["ck_source_control_mr_binding_kind"]
    assert "dev" in binding_checks["ck_source_control_mr_binding_target"]
