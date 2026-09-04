"""Add formal task-to-main delivery projections."""

from alembic import op

revision = "0007_req_formal_delivery"
down_revision = "0006_req_evidence_acceptance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE requirement.work_item "
        "ADD COLUMN formal_delivery_state TEXT NOT NULL DEFAULT 'NOT_STARTED', "
        "ADD COLUMN formal_merge_request_binding_id UUID, "
        "ADD COLUMN formal_blocked_reason_code TEXT, "
        "ADD COLUMN formal_updated_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE requirement.work_item "
        "ALTER COLUMN formal_delivery_state DROP DEFAULT, "
        "ADD CONSTRAINT ck_req_work_item_formal_delivery_state CHECK ("
        "formal_delivery_state IN ('NOT_STARTED', 'MR_PENDING', 'MR_OPEN', "
        "'MERGE_PENDING', 'MERGED', 'BLOCKED', 'RECONCILIATION_PENDING')), "
        "ADD CONSTRAINT ck_req_work_item_formal_binding CHECK ("
        "(formal_delivery_state='NOT_STARTED' "
        "AND formal_merge_request_binding_id IS NULL) OR "
        "formal_delivery_state='MR_PENDING' OR "
        "(formal_delivery_state IN ('MR_OPEN', 'MERGE_PENDING', 'MERGED') "
        "AND formal_merge_request_binding_id IS NOT NULL "
        "AND formal_blocked_reason_code IS NULL) OR "
        "(formal_delivery_state IN ('BLOCKED', 'RECONCILIATION_PENDING'))), "
        "ADD CONSTRAINT ck_req_work_item_formal_block CHECK ("
        "(formal_delivery_state='BLOCKED' "
        "AND formal_blocked_reason_code IN ("
        "'MERGE_ACTOR_INELIGIBLE', 'REPOSITORY_NOT_AUTHORIZED', "
        "'BRANCH_BINDING_MISSING', "
        "'TARGET_BRANCH_NOT_FOUND', 'TARGET_BRANCH_NOT_PROTECTED', "
        "'NO_DELIVERY_COMMIT', 'HEAD_SHA_CHANGED', 'MR_CONFLICT', 'MR_CLOSED', "
        "'MR_CHECKS_BLOCKED', 'MERGE_CONFLICT', 'PROJECT_PROFILE_UNSUPPORTED', "
        "'SOURCE_BRANCH_MISSING_AFTER_INTEGRATION', 'EXTERNAL_MERGE_DRIFT')) OR "
        "(formal_delivery_state<>'BLOCKED' AND formal_blocked_reason_code IS NULL)), "
        "ADD CONSTRAINT ck_req_work_item_formal_completion CHECK ("
        "formal_delivery_state<>'MERGED' OR state='COMPLETED')"
    )
    op.execute("REVOKE UPDATE ON requirement.work_item FROM requirement_rw")
    op.execute(
        "GRANT UPDATE (state, human_owner_id, executor_id, assignment_state, "
        "repository_state, base_commit_sha, task_branch, repository_blocked_reason_code, "
        "repository_blocked_at, integration_delivery_state, "
        "integration_merge_request_binding_id, integration_blocked_reason_code, "
        "integration_updated_at, formal_delivery_state, formal_merge_request_binding_id, "
        "formal_blocked_reason_code, formal_updated_at, revision, updated_at) "
        "ON requirement.work_item TO requirement_rw"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM requirement.work_item
                WHERE formal_delivery_state <> 'NOT_STARTED'
                   OR formal_merge_request_binding_id IS NOT NULL
                   OR formal_blocked_reason_code IS NOT NULL
                   OR formal_updated_at IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM requirement.delivery_gate
                WHERE gate_type='FORMAL_MR_REVIEW'
            )
            THEN
                RAISE EXCEPTION 'formal delivery facts prevent Requirement downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "REVOKE UPDATE (state, human_owner_id, executor_id, assignment_state, "
        "repository_state, base_commit_sha, task_branch, repository_blocked_reason_code, "
        "repository_blocked_at, integration_delivery_state, "
        "integration_merge_request_binding_id, integration_blocked_reason_code, "
        "integration_updated_at, formal_delivery_state, formal_merge_request_binding_id, "
        "formal_blocked_reason_code, formal_updated_at, revision, updated_at) "
        "ON requirement.work_item FROM requirement_rw"
    )
    op.execute("GRANT UPDATE ON requirement.work_item TO requirement_rw")
    op.execute(
        "ALTER TABLE requirement.work_item "
        "DROP CONSTRAINT ck_req_work_item_formal_completion, "
        "DROP CONSTRAINT ck_req_work_item_formal_block, "
        "DROP CONSTRAINT ck_req_work_item_formal_binding, "
        "DROP CONSTRAINT ck_req_work_item_formal_delivery_state, "
        "DROP COLUMN formal_updated_at, "
        "DROP COLUMN formal_blocked_reason_code, "
        "DROP COLUMN formal_merge_request_binding_id, "
        "DROP COLUMN formal_delivery_state"
    )
