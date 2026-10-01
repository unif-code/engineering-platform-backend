"""Single-send model checks owned by Model Gateway, separate worker privileges."""

from alembic import op

revision = "0002_model_connection_checks"
down_revision = "0001_model_gateway_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE model_gateway.connection_check (
            id UUID PRIMARY KEY,
            deployment_id UUID NOT NULL REFERENCES model_gateway.deployment(id),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            requested_by TEXT NOT NULL CHECK (length(requested_by) > 0),
            requested_at TIMESTAMPTZ NOT NULL,
            input JSONB NOT NULL CHECK (jsonb_typeof(input)='object'),
            connection_ref TEXT,
            state TEXT NOT NULL CHECK (state IN
                ('QUEUED','RUNNING','SUCCEEDED','FAILED','UNKNOWN','BLOCKED')),
            reason TEXT,
            attempt INTEGER NOT NULL CHECK (attempt IN (0,1)),
            execution_token UUID,
            started_at TIMESTAMPTZ,
            deadline_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            elapsed_ms INTEGER CHECK (elapsed_ms >= 0),
            provider_request_id TEXT,
            reported_model_id TEXT,
            usage JSONB CHECK (jsonb_typeof(usage)='object'),
            material_currentness TEXT NOT NULL
                CHECK (material_currentness IN ('CURRENT','STALE','UNVERIFIABLE')),
            CHECK (connection_ref IS NOT DISTINCT FROM (input->>'connection_ref')),
            CHECK ((attempt=0 AND execution_token IS NULL
                       AND started_at IS NULL AND deadline_at IS NULL)
                OR (attempt=1 AND execution_token IS NOT NULL AND started_at IS NOT NULL
                    AND deadline_at > started_at AND connection_ref IS NOT NULL)),
            CHECK ((state IN ('QUEUED','BLOCKED') AND attempt=0)
                OR (state IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN') AND attempt=1)),
            CHECK ((state IN ('QUEUED','RUNNING') AND finished_at IS NULL AND reason IS NULL)
                OR (state='SUCCEEDED' AND finished_at IS NOT NULL AND reason IS NULL)
                OR (state IN ('FAILED','UNKNOWN','BLOCKED')
                    AND finished_at IS NOT NULL AND reason IS NOT NULL)),
            CHECK (state IN ('SUCCEEDED','FAILED')
                OR (provider_request_id IS NULL AND reported_model_id IS NULL AND usage IS NULL))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_model_connection_active_candidate
        ON model_gateway.connection_check(deployment_id) WHERE state IN ('QUEUED','RUNNING')
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_model_connection_running_connection
        ON model_gateway.connection_check(connection_ref) WHERE state='RUNNING'
    """)
    op.execute("""
        CREATE INDEX ix_model_connection_history
        ON model_gateway.connection_check(deployment_id, requested_at DESC, id DESC)
    """)
    op.execute("""
        CREATE FUNCTION model_gateway.guard_connection_check() RETURNS trigger
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
    op.execute("""
        CREATE TRIGGER model_connection_check_guard BEFORE INSERT OR UPDATE
        ON model_gateway.connection_check FOR EACH ROW
        EXECUTE FUNCTION model_gateway.guard_connection_check()
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='model_gateway_worker_rw') THEN
                CREATE ROLE model_gateway_worker_rw NOLOGIN;
            END IF;
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA model_gateway TO model_gateway_worker_rw")
    op.execute(
        "GRANT SELECT ON model_gateway.deployment, model_gateway.connection_check "
        "TO model_gateway_worker_rw"
    )
    op.execute("GRANT SELECT, INSERT ON model_gateway.connection_check TO model_gateway_rw")
    op.execute("""
        GRANT UPDATE (revision,state,reason,attempt,execution_token,started_at,deadline_at,
            finished_at,elapsed_ms,provider_request_id,reported_model_id,usage,material_currentness)
        ON model_gateway.connection_check TO model_gateway_worker_rw
    """)
    op.execute("REVOKE ALL ON FUNCTION model_gateway.guard_connection_check() FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TABLE model_gateway.connection_check")
    op.execute("DROP FUNCTION model_gateway.guard_connection_check()")
    op.execute("REVOKE SELECT ON model_gateway.deployment FROM model_gateway_worker_rw")
    op.execute("REVOKE USAGE ON SCHEMA model_gateway FROM model_gateway_worker_rw")
