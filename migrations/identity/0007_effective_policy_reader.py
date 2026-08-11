"""grant identity runtime read-only access to its effective policy."""

from alembic import op

revision = "0007_identity_policy_reader"
down_revision = "0006_identity_config_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON identity.version TO identity_rw")
    op.execute("GRANT SELECT ON identity.active_pointer TO identity_rw")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON identity.active_pointer FROM identity_rw")
    op.execute("REVOKE SELECT ON identity.version FROM identity_rw")
