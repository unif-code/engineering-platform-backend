"""identity-owned least-privilege policy reauthentication surface."""

from alembic import op

revision = "0010_identity_policy_reauth"
down_revision = "0009_identity_policy_publish"
branch_labels = None
depends_on = None


_CHALLENGE_INSERT_COLUMNS = (
    "id, token_hash, purpose, account_id, actor_id, issued_at, "
    "expires_at, attempt_limit, attempt_count"
)
_CHALLENGE_UPDATE_COLUMNS = "attempt_count, revoked_at, consumed_at"


def upgrade() -> None:
    op.execute("REVOKE INSERT ON identity.version FROM configuration_rw")
    op.execute("REVOKE UPDATE ON identity.active_pointer FROM configuration_rw")
    op.execute("REVOKE SELECT ON identity.configuration_outbox FROM configuration_rw")
    op.execute("REVOKE SELECT ON identity.account FROM configuration_rw")
    op.execute(
        "REVOKE UPDATE (totp_last_step, updated_at) ON identity.account FROM configuration_rw"
    )
    op.execute("REVOKE SELECT ON identity.auth_challenge FROM configuration_rw")
    op.execute(
        f"REVOKE INSERT ({_CHALLENGE_INSERT_COLUMNS}) "
        "ON identity.auth_challenge FROM configuration_rw"
    )
    op.execute(
        f"REVOKE UPDATE ({_CHALLENGE_UPDATE_COLUMNS}) "
        "ON identity.auth_challenge FROM configuration_rw"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON identity.draft TO identity_rw")
    op.execute("GRANT INSERT ON identity.version TO identity_rw")
    op.execute("GRANT UPDATE ON identity.active_pointer TO identity_rw")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON identity.configuration_idempotency_record TO identity_rw"
    )
    op.execute("GRANT INSERT ON identity.configuration_outbox TO identity_rw")


def downgrade() -> None:
    op.execute("REVOKE INSERT ON identity.configuration_outbox FROM identity_rw")
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON "
        "identity.configuration_idempotency_record FROM identity_rw"
    )
    op.execute("REVOKE UPDATE ON identity.active_pointer FROM identity_rw")
    op.execute("REVOKE INSERT ON identity.version FROM identity_rw")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON identity.draft FROM identity_rw")
    op.execute("GRANT INSERT ON identity.version TO configuration_rw")
    op.execute("GRANT UPDATE ON identity.active_pointer TO configuration_rw")
    op.execute("GRANT SELECT ON identity.configuration_outbox TO configuration_rw")
    op.execute("GRANT SELECT ON identity.account TO configuration_rw")
    op.execute("GRANT UPDATE (totp_last_step, updated_at) ON identity.account TO configuration_rw")
    op.execute("GRANT SELECT ON identity.auth_challenge TO configuration_rw")
    op.execute(
        f"GRANT INSERT ({_CHALLENGE_INSERT_COLUMNS}) ON identity.auth_challenge TO configuration_rw"
    )
    op.execute(
        f"GRANT UPDATE ({_CHALLENGE_UPDATE_COLUMNS}) ON identity.auth_challenge TO configuration_rw"
    )
