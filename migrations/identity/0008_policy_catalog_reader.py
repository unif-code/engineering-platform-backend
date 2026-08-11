"""grant identity runtime read-only access to its policy schema catalog."""

from alembic import op

revision = "0008_identity_policy_catalog"
down_revision = "0007_identity_policy_reader"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON identity.policy_key TO identity_rw")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON identity.policy_key FROM identity_rw")
