"""Add safe merge-request webhook summaries for reconciliation hints."""

from alembic import op

revision = "0006_sc_mr_reconcile"
down_revision = "0005_sc_int_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE source_control.webhook_inbox "
        "ADD COLUMN mr_iid BIGINT, "
        "ADD COLUMN mr_action TEXT, "
        "ADD COLUMN source_branch TEXT, "
        "ADD COLUMN target_branch TEXT, "
        "ADD COLUMN mr_state TEXT, "
        "ADD COLUMN old_head_sha TEXT, "
        "ADD COLUMN head_sha TEXT"
    )
    op.execute(
        """
        ALTER TABLE source_control.webhook_inbox
        ADD CONSTRAINT ck_source_control_webhook_mr_shape CHECK (
            (
                mr_iid IS NULL
                AND mr_action IS NULL
                AND source_branch IS NULL
                AND target_branch IS NULL
                AND mr_state IS NULL
                AND old_head_sha IS NULL
                AND head_sha IS NULL
            )
            OR (
                mr_iid IS NOT NULL
                AND mr_iid >= 1
                AND mr_action IN ('open', 'update', 'merge', 'close', 'reopen')
                AND source_branch IS NOT NULL
                AND target_branch IS NOT NULL
                AND mr_state IN ('opened', 'merged', 'closed', 'locked')
                AND head_sha IS NOT NULL
            )
        ),
        ADD CONSTRAINT ck_source_control_webhook_mr_refs CHECK (
            (
                mr_iid IS NULL
                AND mr_action IS NULL
                AND source_branch IS NULL
                AND target_branch IS NULL
                AND mr_state IS NULL
                AND old_head_sha IS NULL
                AND head_sha IS NULL
            )
            OR (
                length(btrim(source_branch)) > 0
                AND length(btrim(target_branch)) > 0
                AND head_sha ~ '^[0-9a-f]{40}$'
                AND (
                    old_head_sha IS NULL
                    OR old_head_sha ~ '^[0-9a-f]{40}$'
                )
            )
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON source_control.webhook_inbox TO source_control_rw")


def downgrade() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM source_control.webhook_inbox
                WHERE mr_iid IS NOT NULL
            )
            THEN
                RAISE EXCEPTION
                    'MR webhook summaries prevent Source Control downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "ALTER TABLE source_control.webhook_inbox "
        "DROP CONSTRAINT ck_source_control_webhook_mr_refs, "
        "DROP CONSTRAINT ck_source_control_webhook_mr_shape, "
        "DROP COLUMN head_sha, "
        "DROP COLUMN old_head_sha, "
        "DROP COLUMN mr_state, "
        "DROP COLUMN target_branch, "
        "DROP COLUMN source_branch, "
        "DROP COLUMN mr_action, "
        "DROP COLUMN mr_iid"
    )
