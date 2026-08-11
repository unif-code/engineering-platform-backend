"""persist a bounded TOTP failure counter on bootstrap sessions."""

from alembic import op

revision = "0004_identity_bootstrap_totp_cap"
down_revision = "0003_identity_bootstrap_purpose"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE identity.session "
        "ADD COLUMN bootstrap_totp_attempt_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        """
        ALTER TABLE identity.session
        ADD CONSTRAINT ck_identity_session_bootstrap_totp_attempt_count
        CHECK (
            bootstrap_totp_attempt_count >= 0
            AND (kind = 'BOOTSTRAP' OR bootstrap_totp_attempt_count = 0)
        )
        """
    )
    op.execute(
        "UPDATE identity.session SET revoked_at=now(), "
        "revoke_reason='MIGRATION_BOOTSTRAP_TOTP_ATTEMPTS_UPGRADE' "
        "WHERE kind='BOOTSTRAP' AND revoked_at IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE identity.session SET revoked_at=now(), "
        "revoke_reason='MIGRATION_BOOTSTRAP_TOTP_ATTEMPTS_DOWNGRADE' "
        "WHERE kind='BOOTSTRAP' AND revoked_at IS NULL"
    )
    op.execute(
        "ALTER TABLE identity.session "
        "DROP CONSTRAINT ck_identity_session_bootstrap_totp_attempt_count"
    )
    op.execute("ALTER TABLE identity.session DROP COLUMN bootstrap_totp_attempt_count")
