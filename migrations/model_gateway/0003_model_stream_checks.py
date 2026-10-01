"""Add fixed stream kinds without rewriting frozen input, receipts or lifecycle state."""

from alembic import op

revision = "0003_model_stream_checks"
down_revision = "0002_model_connection_checks"
branch_labels = None
depends_on = None

_OBSERVATION_COLUMNS = (
    "observed_bytes",
    "observed_text",
    "observed_normal_completion",
    "observed_data_events",
    "observed_text_deltas",
    "observed_text_bytes",
    "observed_completion_marker",
    "observed_local_closed",
    "provider_cancellation",
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD COLUMN check_kind TEXT NOT NULL DEFAULT 'BASIC_TEXT' CHECK "
        "(check_kind IN ('BASIC_TEXT','STREAM_TEXT','STREAM_STOP'))"
    )
    op.execute("ALTER TABLE model_gateway.connection_check ALTER COLUMN check_kind DROP DEFAULT")
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD COLUMN observed_bytes INTEGER CHECK (observed_bytes BETWEEN 0 AND 65537)"
    )
    op.execute("ALTER TABLE model_gateway.connection_check ADD COLUMN observed_text BOOLEAN")
    op.execute(
        "ALTER TABLE model_gateway.connection_check ADD COLUMN observed_normal_completion BOOLEAN"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD COLUMN observed_data_events INTEGER CHECK (observed_data_events BETWEEN 0 AND 257)"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD COLUMN observed_text_deltas INTEGER CHECK (observed_text_deltas BETWEEN 0 AND 256)"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD COLUMN observed_text_bytes INTEGER CHECK (observed_text_bytes BETWEEN 0 AND 65536)"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check ADD COLUMN observed_completion_marker BOOLEAN"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check ADD COLUMN observed_local_closed BOOLEAN"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD COLUMN provider_cancellation TEXT CHECK (provider_cancellation='UNCONFIRMED')"
    )
    op.execute("""
        DO $$ DECLARE constraint_name TEXT; BEGIN
            SELECT conname INTO STRICT constraint_name FROM pg_constraint
            WHERE conrelid='model_gateway.connection_check'::regclass
              AND contype='c' AND pg_get_constraintdef(oid) LIKE '%provider_request_id IS NULL%';
            EXECUTE format('ALTER TABLE model_gateway.connection_check DROP CONSTRAINT %I',
                           constraint_name);
        END $$
    """)
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT ck_model_check_observed_metadata
        CHECK (state IN ('SUCCEEDED','FAILED','UNKNOWN')
            OR (provider_request_id IS NULL AND reported_model_id IS NULL AND usage IS NULL))
    """)
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
    op.execute("""
        CREATE OR REPLACE FUNCTION model_gateway.guard_connection_check() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.revision<>1 OR NEW.state NOT IN ('QUEUED','BLOCKED')
                THEN RAISE EXCEPTION 'invalid check admission'; END IF;
            ELSE
                IF OLD.state NOT IN ('QUEUED','RUNNING') OR NEW.revision<>OLD.revision+1
                   OR (NEW.id,NEW.deployment_id,NEW.requested_by,NEW.requested_at,
                       NEW.input,NEW.connection_ref,NEW.check_kind)
                       IS DISTINCT FROM
                      (OLD.id,OLD.deployment_id,OLD.requested_by,OLD.requested_at,OLD.input,OLD.connection_ref,OLD.check_kind)
                   OR (OLD.state='QUEUED' AND NEW.state NOT IN ('RUNNING','BLOCKED'))
                   OR (OLD.state='RUNNING' AND (
                       NEW.state NOT IN ('SUCCEEDED','FAILED','UNKNOWN') OR
                       (NEW.attempt,NEW.execution_token,NEW.started_at,NEW.deadline_at)
                         IS DISTINCT FROM
                       (OLD.attempt,OLD.execution_token,OLD.started_at,OLD.deadline_at)))
                THEN RAISE EXCEPTION 'invalid check transition'; END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "GRANT UPDATE ("
        + ",".join(_OBSERVATION_COLUMNS)
        + ") ON model_gateway.connection_check TO model_gateway_worker_rw"
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM model_gateway.connection_check
                       WHERE check_kind<>'BASIC_TEXT' OR observed_bytes IS NOT NULL)
            THEN RAISE EXCEPTION 'stream check facts prevent downgrade'; END IF;
        END $$
    """)
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT ck_model_check_observation_shape"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT ck_model_check_observed_metadata"
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CHECK (state IN ('SUCCEEDED','FAILED')
            OR (provider_request_id IS NULL AND reported_model_id IS NULL AND usage IS NULL))
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION model_gateway.guard_connection_check() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.revision<>1 OR NEW.state NOT IN ('QUEUED','BLOCKED')
                THEN RAISE EXCEPTION 'invalid check admission'; END IF;
            ELSE
                IF OLD.state NOT IN ('QUEUED','RUNNING') OR NEW.revision<>OLD.revision+1
                   OR (NEW.id,NEW.deployment_id,NEW.requested_by,NEW.requested_at,
                       NEW.input,NEW.connection_ref)
                       IS DISTINCT FROM
                      (OLD.id,OLD.deployment_id,OLD.requested_by,OLD.requested_at,OLD.input,OLD.connection_ref)
                   OR (OLD.state='QUEUED' AND NEW.state NOT IN ('RUNNING','BLOCKED'))
                   OR (OLD.state='RUNNING' AND (
                       NEW.state NOT IN ('SUCCEEDED','FAILED','UNKNOWN') OR
                       (NEW.attempt,NEW.execution_token,NEW.started_at,NEW.deadline_at)
                         IS DISTINCT FROM
                       (OLD.attempt,OLD.execution_token,OLD.started_at,OLD.deadline_at)))
                THEN RAISE EXCEPTION 'invalid check transition'; END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    for column in _OBSERVATION_COLUMNS:
        op.execute(f"ALTER TABLE model_gateway.connection_check DROP COLUMN {column}")
    op.execute("ALTER TABLE model_gateway.connection_check DROP COLUMN check_kind")
