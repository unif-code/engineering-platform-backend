"""Add typed reasoning observations without rewriting any historical fact or receipt."""

from alembic import op

revision = "0004_model_thinking_checks"
down_revision = "0003_model_stream_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT connection_check_check_kind_check"
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT connection_check_check_kind_check
        CHECK (check_kind IN ('BASIC_TEXT','STREAM_TEXT','STREAM_STOP','THINKING'))
    """)
    op.execute("ALTER TABLE model_gateway.connection_check ADD COLUMN observed_reasoning BOOLEAN")
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD COLUMN observed_reasoning_deltas INTEGER
        CHECK (observed_reasoning_deltas BETWEEN 0 AND 256)
    """)
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD COLUMN observed_reasoning_bytes INTEGER
        CHECK (observed_reasoning_bytes BETWEEN 0 AND 65536)
    """)
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT ck_model_check_observation_shape"
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT ck_model_check_observation_shape
        CHECK (
            (observed_bytes IS NULL AND observed_text IS NULL AND observed_normal_completion IS NULL
             AND observed_data_events IS NULL AND observed_text_deltas IS NULL
             AND observed_text_bytes IS NULL
             AND observed_completion_marker IS NULL AND observed_local_closed IS NULL
             AND provider_cancellation IS NULL
             AND observed_reasoning IS NULL AND observed_reasoning_deltas IS NULL
             AND observed_reasoning_bytes IS NULL)
            OR (state IN ('SUCCEEDED','FAILED','UNKNOWN') AND observed_bytes IS NOT NULL
                AND observed_text IS NOT NULL AND observed_normal_completion IS NOT NULL AND (
                  (check_kind='BASIC_TEXT' AND observed_data_events IS NULL
                   AND observed_text_deltas IS NULL
                   AND observed_text_bytes IS NULL AND observed_completion_marker IS NULL
                   AND observed_local_closed IS NULL AND provider_cancellation IS NULL
                   AND observed_reasoning IS NULL AND observed_reasoning_deltas IS NULL
                   AND observed_reasoning_bytes IS NULL)
                  OR
                  (check_kind IN ('STREAM_TEXT','STREAM_STOP','THINKING')
                   AND observed_data_events IS NOT NULL
                   AND observed_text_deltas IS NOT NULL AND observed_text_bytes IS NOT NULL
                   AND observed_completion_marker IS NOT NULL AND observed_local_closed IS NOT NULL
                   AND provider_cancellation IS NOT NULL AND provider_cancellation='UNCONFIRMED'
                   AND ((check_kind='THINKING' AND observed_reasoning IS NOT NULL
                         AND observed_reasoning_deltas IS NOT NULL
                         AND observed_reasoning_bytes IS NOT NULL)
                        OR (check_kind<>'THINKING' AND observed_reasoning IS NULL
                            AND observed_reasoning_deltas IS NULL
                            AND observed_reasoning_bytes IS NULL)))
                ))
        )
    """)
    op.execute("""
        GRANT UPDATE (observed_reasoning,observed_reasoning_deltas,observed_reasoning_bytes)
        ON model_gateway.connection_check TO model_gateway_worker_rw
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM model_gateway.connection_check WHERE check_kind='THINKING')
            THEN RAISE EXCEPTION 'thinking check facts prevent downgrade'; END IF;
        END $$
    """)
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT ck_model_check_observation_shape"
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT ck_model_check_observation_shape
        CHECK (
            (observed_bytes IS NULL AND observed_text IS NULL AND observed_normal_completion IS NULL
             AND observed_data_events IS NULL AND observed_text_deltas IS NULL
             AND observed_text_bytes IS NULL
             AND observed_completion_marker IS NULL AND observed_local_closed IS NULL
             AND provider_cancellation IS NULL)
            OR (state IN ('SUCCEEDED','FAILED','UNKNOWN') AND observed_bytes IS NOT NULL
                AND observed_text IS NOT NULL AND observed_normal_completion IS NOT NULL AND (
                  (check_kind='BASIC_TEXT' AND observed_data_events IS NULL
                   AND observed_text_deltas IS NULL
                   AND observed_text_bytes IS NULL AND observed_completion_marker IS NULL
                   AND observed_local_closed IS NULL AND provider_cancellation IS NULL)
                  OR
                  (check_kind IN ('STREAM_TEXT','STREAM_STOP') AND observed_data_events IS NOT NULL
                   AND observed_text_deltas IS NOT NULL AND observed_text_bytes IS NOT NULL
                   AND observed_completion_marker IS NOT NULL AND observed_local_closed IS NOT NULL
                   AND provider_cancellation IS NOT NULL AND provider_cancellation='UNCONFIRMED')
                ))
        )
    """)
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT connection_check_check_kind_check"
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT connection_check_check_kind_check
        CHECK (check_kind IN ('BASIC_TEXT','STREAM_TEXT','STREAM_STOP'))
    """)
    op.execute("ALTER TABLE model_gateway.connection_check DROP COLUMN observed_reasoning")
    op.execute("ALTER TABLE model_gateway.connection_check DROP COLUMN observed_reasoning_deltas")
    op.execute("ALTER TABLE model_gateway.connection_check DROP COLUMN observed_reasoning_bytes")
