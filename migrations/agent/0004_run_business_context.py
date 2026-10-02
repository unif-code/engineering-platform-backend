"""Preserve the Requirement owner's startup identities without backfilling Run history."""

from alembic import op

revision = "0004_run_business_context"
down_revision = "0003_event_acceptance_receipt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE agent.agent_run
            ADD COLUMN requirement_id UUID,
            ADD COLUMN work_item_id UUID,
            ADD COLUMN assignment_id UUID,
            ADD CONSTRAINT ck_agent_run_business_context
                CHECK (num_nonnulls(requirement_id, work_item_id, assignment_id) IN (0, 3))
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent.agent_run WHERE requirement_id IS NOT NULL) THEN
                RAISE EXCEPTION 'refusing Agent downgrade: business source snapshots still exist';
            END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE agent.agent_run
            DROP CONSTRAINT ck_agent_run_business_context,
            DROP COLUMN requirement_id,
            DROP COLUMN work_item_id,
            DROP COLUMN assignment_id
    """)
