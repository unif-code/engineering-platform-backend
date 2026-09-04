"""Identity-owned immutable exact policy authentication consumption facts."""

from alembic import op

revision = "0011_identity_reauth_consumption"
down_revision = "0010_identity_policy_reauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE identity.policy_reauth_consumption (
            id UUID PRIMARY KEY,
            actor_id UUID NOT NULL,
            attempt_id TEXT NOT NULL CHECK (length(attempt_id) > 0),
            binding JSONB NOT NULL CHECK (jsonb_typeof(binding) = 'object'),
            binding_hash TEXT NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
            session_reference UUID NOT NULL,
            account_version BIGINT NOT NULL CHECK (account_version > 0),
            consumed_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            UNIQUE (actor_id, attempt_id),
            CHECK (expires_at = consumed_at + interval '5 minutes'),
            CHECK (binding->>'actor_id' = actor_id::text),
            CHECK (binding->>'command_attempt_id' = attempt_id),
            CHECK (binding->>'operation' IN ('POLICY_PUBLISH', 'POLICY_ROLLBACK'))
        )
    """)
    op.execute("GRANT SELECT, INSERT ON identity.policy_reauth_consumption TO identity_rw")


def downgrade() -> None:
    op.execute("DROP TABLE identity.policy_reauth_consumption")
