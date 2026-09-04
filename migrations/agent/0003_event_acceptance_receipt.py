"""Preserve immutable original Agent event acceptances without backfilling history."""

from alembic import op

revision = "0003_event_acceptance_receipt"
down_revision = "0002_workflow_claim_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE agent.event_acceptance_receipt ("
        "event_id UUID PRIMARY KEY REFERENCES agent.canonical_event(event_id), "
        "schema_version INTEGER NOT NULL CHECK (schema_version=1), "
        "attempt JSONB NOT NULL CHECK (jsonb_typeof(attempt)='object'), "
        "checkpoint JSONB CHECK (jsonb_typeof(checkpoint)='object'))"
    )
    op.execute("GRANT SELECT, INSERT ON agent.event_acceptance_receipt TO agent_rw")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM agent.event_acceptance_receipt) THEN "
        "RAISE EXCEPTION 'refusing Agent receipt downgrade: durable receipts still exist'; "
        "END IF; END $$"
    )
    op.execute("DROP TABLE agent.event_acceptance_receipt")
