"""Metadata observations share the existing worker admission lock, not inference results."""

from alembic import op

revision = "0008_model_metadata_observations"
down_revision = "0007_model_material_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE model_gateway.metadata_observation (
            id UUID PRIMARY KEY,
            deployment_id UUID NOT NULL REFERENCES model_gateway.deployment(id),
            revision INTEGER NOT NULL CHECK (revision>=1),
            requested_by TEXT NOT NULL CHECK (length(requested_by)>0),
            requested_at TIMESTAMPTZ NOT NULL,
            input JSONB NOT NULL CHECK (jsonb_typeof(input)='object'),
            connection_ref TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'QUEUED','RUNNING','REPORTED','NOT_FOUND','INDETERMINATE','FAILED','BLOCKED','UNKNOWN')),
            reason TEXT,
            attempt INTEGER NOT NULL CHECK (attempt IN (0,1)),
            execution_token UUID,
            started_at TIMESTAMPTZ,
            deadline_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            elapsed_ms INTEGER CHECK (elapsed_ms>=0),
            response_sha256 TEXT CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
            response_bytes INTEGER CHECK (response_bytes BETWEEN 0 AND 65536),
            provider_request_id TEXT CHECK (length(provider_request_id)<=128),
            report JSONB CHECK (jsonb_typeof(report)='object'),
            material_currentness TEXT NOT NULL
                CHECK (material_currentness IN ('MATCHING','STALE','UNVERIFIABLE')),
            CHECK (connection_ref IS NOT DISTINCT FROM input->>'connection_ref'),
            CHECK (deployment_id::text IS NOT DISTINCT FROM input->>'deployment_id'),
            CHECK (input ?& ARRAY[
                'deployment_revision','source','material_version','input_digest']),
            CHECK ((jsonb_typeof(input->'source')='object'
                AND input->>'input_digest' ~ '^[0-9a-f]{64}$'
                AND input->'source'->>'fingerprint' ~ '^[0-9a-f]{64}$') IS TRUE),
            CHECK ((response_sha256 IS NULL)=(response_bytes IS NULL)),
            CHECK ((state='REPORTED')=(report IS NOT NULL)),
            CHECK ((attempt=0 AND execution_token IS NULL
                    AND started_at IS NULL AND deadline_at IS NULL)
                OR (attempt=1 AND execution_token IS NOT NULL
                    AND started_at IS NOT NULL AND deadline_at>started_at)),
            CHECK ((state IN ('QUEUED','BLOCKED') AND attempt=0)
                OR (state IN ('RUNNING','REPORTED','NOT_FOUND','INDETERMINATE','FAILED','UNKNOWN')
                    AND attempt=1)),
            CHECK ((state IN ('QUEUED','RUNNING') AND finished_at IS NULL AND reason IS NULL)
                OR (state IN ('REPORTED','NOT_FOUND')
                    AND finished_at IS NOT NULL AND reason IS NULL)
                OR (state IN ('INDETERMINATE','FAILED','BLOCKED','UNKNOWN')
                    AND finished_at IS NOT NULL AND reason IS NOT NULL)),
            CHECK (state NOT IN ('QUEUED','RUNNING','BLOCKED') OR
                (response_sha256 IS NULL AND provider_request_id IS NULL AND report IS NULL))
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX uq_model_metadata_active_candidate "
        "ON model_gateway.metadata_observation(deployment_id) WHERE state IN ('QUEUED','RUNNING')"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_model_metadata_running_connection "
        "ON model_gateway.metadata_observation(connection_ref) WHERE state='RUNNING'"
    )
    op.execute(
        "CREATE INDEX ix_model_metadata_history "
        "ON model_gateway.metadata_observation(deployment_id,requested_at DESC,id DESC)"
    )
    op.execute("""
        CREATE FUNCTION model_gateway.guard_metadata_observation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE candidate model_gateway.deployment;
        BEGIN
            IF TG_OP='DELETE' THEN RAISE EXCEPTION 'metadata history cannot be deleted'; END IF;
            IF TG_OP='INSERT' THEN
                IF NEW.revision<>1 OR NEW.state<>'QUEUED'
                THEN RAISE EXCEPTION 'invalid metadata admission'; END IF;
                SELECT * INTO candidate FROM model_gateway.deployment
                    WHERE id=NEW.deployment_id FOR UPDATE;
                IF NOT FOUND OR candidate.state<>'DRAFT' OR
                    (candidate.revision,candidate.provider_kind,candidate.provider_model_id,
                     candidate.connection_ref,
                     NULLIF(to_jsonb(candidate.declared_context_window),'null'::jsonb),
                     NULLIF(to_jsonb(candidate.declared_max_output_tokens),'null'::jsonb))
                    IS DISTINCT FROM
                    ((NEW.input->>'deployment_revision')::integer,NEW.input->>'provider_kind',
                     NEW.input->>'provider_model_id',NEW.connection_ref,
                     NULLIF(NEW.input->'declared_context_window','null'::jsonb),
                     NULLIF(NEW.input->'declared_max_output_tokens','null'::jsonb))
                THEN RAISE EXCEPTION 'metadata candidate binding changed'; END IF;
            ELSE
                IF OLD.state NOT IN ('QUEUED','RUNNING') OR NEW.revision<>OLD.revision+1
                    OR (NEW.id,NEW.deployment_id,NEW.requested_by,NEW.requested_at,
                        NEW.input,NEW.connection_ref)
                        IS DISTINCT FROM
                       (OLD.id,OLD.deployment_id,OLD.requested_by,OLD.requested_at,OLD.input,OLD.connection_ref)
                    OR (OLD.state='QUEUED' AND NEW.state NOT IN ('RUNNING','BLOCKED'))
                    OR (OLD.state='RUNNING' AND (
                        NEW.state NOT IN
                            ('REPORTED','NOT_FOUND','INDETERMINATE','FAILED','UNKNOWN') OR
                        (NEW.attempt,NEW.execution_token,NEW.started_at,NEW.deadline_at)
                        IS DISTINCT FROM
                        (OLD.attempt,OLD.execution_token,OLD.started_at,OLD.deadline_at)))
                THEN RAISE EXCEPTION 'invalid metadata transition'; END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER metadata_observation_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON model_gateway.metadata_observation FOR EACH ROW "
        "EXECUTE FUNCTION model_gateway.guard_metadata_observation()"
    )
    op.execute("GRANT SELECT,INSERT ON model_gateway.metadata_observation TO model_gateway_rw")
    op.execute("GRANT SELECT ON model_gateway.metadata_observation TO model_gateway_worker_rw")
    op.execute("""
        GRANT UPDATE (revision,state,reason,attempt,execution_token,started_at,deadline_at,
            finished_at,elapsed_ms,response_sha256,response_bytes,provider_request_id,report,material_currentness)
        ON model_gateway.metadata_observation TO model_gateway_worker_rw
    """)
    op.execute("REVOKE ALL ON FUNCTION model_gateway.guard_metadata_observation() FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TABLE model_gateway.metadata_observation")
    op.execute("DROP FUNCTION model_gateway.guard_metadata_observation()")
