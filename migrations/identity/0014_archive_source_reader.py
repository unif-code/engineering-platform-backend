"""Allow the Identity owner to read only persisted archive event business columns."""

from alembic import op

revision = "0014_identity_archive_source"
down_revision = "0013_identity_draft_clone"
branch_labels = None
depends_on = None

_COLUMNS = "id,namespace,scope,event_type,aggregate_id,payload,occurred_at"


def upgrade() -> None:
    op.execute(f"GRANT SELECT ({_COLUMNS}) ON identity.configuration_outbox TO identity_rw")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ({_COLUMNS}) ON identity.configuration_outbox FROM identity_rw")
