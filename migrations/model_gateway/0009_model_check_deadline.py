"""Require an existing deadline for attempted checks without rewriting historical rows."""

from alembic import op

revision = "0009_model_check_deadline"
down_revision = "0008_model_metadata_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Validate existing rows in the migration transaction. Missing historical deadlines
    # must stop the upgrade; they cannot be reconstructed from a current timeout setting.
    op.execute("""
        ALTER TABLE model_gateway.connection_check
        ADD CONSTRAINT ck_model_check_deadline_present
        CHECK (attempt <> 1 OR deadline_at IS NOT NULL)
    """)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE model_gateway.connection_check DROP CONSTRAINT ck_model_check_deadline_present"
    )
