"""persist an unforgeable purpose for every bootstrap session."""

from alembic import op

revision = "0003_identity_bootstrap_purpose"
down_revision = "0002_identity_backoff_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE identity.session ADD COLUMN bootstrap_purpose TEXT")
    op.execute(
        "UPDATE identity.session SET bootstrap_purpose='INITIAL_SETUP' WHERE kind='BOOTSTRAP'"
    )
    op.execute(
        """
        ALTER TABLE identity.session
        ADD CONSTRAINT ck_identity_session_bootstrap_purpose
        CHECK (
            (kind = 'FULL' AND bootstrap_purpose IS NULL)
            OR (
                kind = 'BOOTSTRAP'
                AND bootstrap_purpose IS NOT NULL
                AND bootstrap_purpose IN (
                    'INITIAL_SETUP', 'PASSWORD_RESET', 'PASSWORD_EXPIRED'
                )
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE identity.session DROP CONSTRAINT ck_identity_session_bootstrap_purpose")
    op.execute("ALTER TABLE identity.session DROP COLUMN bootstrap_purpose")
