"""identity-owned durable idempotency for configuration commands."""

from alembic import op

revision = "0006_identity_config_idempotency"
down_revision = "0005_identity_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE identity.configuration_idempotency_record (
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
            CONSTRAINT uq_identity_configuration_idempotency_scope
                UNIQUE (actor, operation, idempotency_key),
            CONSTRAINT ck_identity_configuration_idempotency_actor
                CHECK (length(btrim(actor)) > 0),
            CONSTRAINT ck_identity_configuration_idempotency_operation
                CHECK (length(btrim(operation)) > 0),
            CONSTRAINT ck_identity_configuration_idempotency_key
                CHECK (length(btrim(idempotency_key)) > 0),
            CONSTRAINT ck_identity_configuration_idempotency_fingerprint
                CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_identity_configuration_idempotency_state
                CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
            CONSTRAINT ck_identity_configuration_idempotency_result
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
                        AND http_status BETWEEN 100 AND 599
                        AND jsonb_typeof(result_metadata) = 'object'
                        AND sealed_response IS NOT NULL
                        AND completed_at IS NOT NULL
                    )
                ),
            CONSTRAINT ck_identity_configuration_idempotency_timestamps
                CHECK (
                    updated_at >= created_at
                    AND (
                        completed_at IS NULL
                        OR (completed_at >= created_at AND completed_at <= updated_at)
                    )
                )
        )
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON "
        "identity.configuration_idempotency_record TO configuration_rw"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON identity.configuration_idempotency_record FROM configuration_rw")
    op.execute("DROP TABLE identity.configuration_idempotency_record")
