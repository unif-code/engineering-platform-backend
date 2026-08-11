"""identity baseline: authentication state, durable commands, and runtime role.

角色口令仅用于本地/CI（生产角色由基础设施子项目管理）。
"""

from alembic import op

revision = "0001_identity_base"
down_revision = None
branch_labels = ("identity",)
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS identity")
    op.execute(
        """
        CREATE TABLE identity.account (
            id UUID PRIMARY KEY,
            employee_no CHAR(8) NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            profession TEXT,
            status TEXT NOT NULL,
            password_hash TEXT,
            password_set_at TIMESTAMPTZ,
            totp_sealed BYTEA,
            totp_confirmed_at TIMESTAMPTZ,
            totp_last_step BIGINT,
            is_super_admin BOOLEAN NOT NULL DEFAULT FALSE,
            version BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_identity_account_employee_no
                CHECK (employee_no ~ '^[0-9]{8}$'),
            CONSTRAINT ck_identity_account_status
                CHECK (status IN ('PENDING_INIT', 'ENABLED', 'DISABLED', 'RESTRICTED')),
            CONSTRAINT ck_identity_account_totp_last_step
                CHECK (totp_last_step IS NULL OR totp_last_step >= 0),
            CONSTRAINT ck_identity_account_version CHECK (version > 0),
            CONSTRAINT ck_identity_account_timestamps CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.temp_credential (
            id UUID PRIMARY KEY,
            account_id UUID NOT NULL REFERENCES identity.account(id),
            secret_hash TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ,
            issued_by UUID NOT NULL REFERENCES identity.account(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_identity_temp_credential_secret_hash
                CHECK (length(secret_hash) > 0),
            CONSTRAINT ck_identity_temp_credential_expiry CHECK (expires_at > created_at),
            CONSTRAINT ck_identity_temp_credential_consumed
                CHECK (consumed_at IS NULL OR consumed_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.session (
            id UUID PRIMARY KEY,
            account_id UUID NOT NULL REFERENCES identity.account(id),
            token_hash TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_hint TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            revoke_reason TEXT,
            CONSTRAINT ck_identity_session_token_hash CHECK (length(token_hash) > 0),
            CONSTRAINT ck_identity_session_kind CHECK (kind IN ('BOOTSTRAP', 'FULL')),
            CONSTRAINT ck_identity_session_last_seen CHECK (last_seen_at >= created_at),
            CONSTRAINT ck_identity_session_expiry CHECK (expires_hint > created_at),
            CONSTRAINT ck_identity_session_revoked
                CHECK (revoked_at IS NULL OR revoked_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.login_backoff (
            employee_no CHAR(8) PRIMARY KEY,
            failure_count INTEGER NOT NULL DEFAULT 0,
            last_failure_at TIMESTAMPTZ,
            locked_until TIMESTAMPTZ,
            CONSTRAINT ck_identity_login_backoff_employee_no
                CHECK (employee_no ~ '^[0-9]{8}$'),
            CONSTRAINT ck_identity_login_backoff_failure_count CHECK (failure_count >= 0),
            CONSTRAINT ck_identity_login_backoff_lock
                CHECK (
                    locked_until IS NULL
                    OR (last_failure_at IS NOT NULL AND locked_until >= last_failure_at)
                )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.auth_challenge (
            id UUID PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL,
            account_id UUID NOT NULL REFERENCES identity.account(id),
            actor_id UUID REFERENCES identity.account(id),
            issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            attempt_limit INTEGER NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            consumed_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            CONSTRAINT ck_identity_auth_challenge_token_hash CHECK (length(token_hash) > 0),
            CONSTRAINT ck_identity_auth_challenge_purpose CHECK (length(purpose) > 0),
            CONSTRAINT ck_identity_auth_challenge_expiry CHECK (expires_at > issued_at),
            CONSTRAINT ck_identity_auth_challenge_attempt_limit CHECK (attempt_limit > 0),
            CONSTRAINT ck_identity_auth_challenge_attempt_count
                CHECK (attempt_count >= 0 AND attempt_count <= attempt_limit),
            CONSTRAINT ck_identity_auth_challenge_consumed
                CHECK (consumed_at IS NULL OR consumed_at >= issued_at),
            CONSTRAINT ck_identity_auth_challenge_revoked
                CHECK (revoked_at IS NULL OR revoked_at >= issued_at),
            CONSTRAINT ck_identity_auth_challenge_terminal
                CHECK (NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.idempotency_record (
            id UUID PRIMARY KEY,
            actor TEXT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            http_status INTEGER,
            result_metadata JSONB,
            sealed_response BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT uq_identity_idempotency_scope
                UNIQUE (actor, operation, idempotency_key),
            CONSTRAINT ck_identity_idempotency_actor CHECK (length(actor) > 0),
            CONSTRAINT ck_identity_idempotency_operation CHECK (length(operation) > 0),
            CONSTRAINT ck_identity_idempotency_key CHECK (length(idempotency_key) > 0),
            CONSTRAINT ck_identity_idempotency_fingerprint
                CHECK (length(request_fingerprint) > 0),
            CONSTRAINT ck_identity_idempotency_state
                CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
            CONSTRAINT ck_identity_idempotency_result
                CHECK (
                    (
                        state = 'IN_PROGRESS'
                        AND http_status IS NULL
                        AND result_metadata IS NULL
                        AND sealed_response IS NULL
                        AND completed_at IS NULL
                    )
                    OR
                    (
                        state = 'COMPLETED'
                        AND http_status IS NOT NULL
                        AND http_status BETWEEN 100 AND 599
                        AND result_metadata IS NOT NULL
                        AND sealed_response IS NOT NULL
                        AND completed_at IS NOT NULL
                    )
                ),
            CONSTRAINT ck_identity_idempotency_timestamps
                CHECK (
                    updated_at >= created_at
                    AND (completed_at IS NULL OR completed_at >= created_at)
                )
        )
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'identity_rw') THEN
                CREATE ROLE identity_rw LOGIN PASSWORD 'localdev';
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA identity TO identity_rw")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA identity TO identity_rw")


def downgrade() -> None:
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA identity FROM identity_rw")
    op.execute("REVOKE USAGE ON SCHEMA identity FROM identity_rw")
    op.execute("DROP TABLE identity.idempotency_record")
    op.execute("DROP TABLE identity.auth_challenge")
    op.execute("DROP TABLE identity.login_backoff")
    op.execute("DROP TABLE identity.session")
    op.execute("DROP TABLE identity.temp_credential")
    op.execute("DROP TABLE identity.account")
    op.execute("DROP SCHEMA identity")
