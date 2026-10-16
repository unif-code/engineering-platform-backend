"""Bound an archive-source lookup without changing or deduplicating existing facts."""

from alembic import op

revision = "0012_req_archive_source"
down_revision = "0011_req_draft_clone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_req_policy_archive_source "
        "ON requirement.gate_policy_outbox (aggregate_id) WHERE event_type='DRAFT_ARCHIVED'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX requirement.ix_req_policy_archive_source")
