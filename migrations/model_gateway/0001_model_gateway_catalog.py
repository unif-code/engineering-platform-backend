"""Persist the Model Gateway candidate catalog; no Provider access or active deployments."""

from alembic import op

revision = "0001_model_gateway_catalog"
down_revision = None
branch_labels = ("model_gateway",)
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA model_gateway")
    op.execute("""
        CREATE TABLE model_gateway.deployment (
            id UUID PRIMARY KEY,
            deployment_key TEXT NOT NULL UNIQUE
                CHECK (length(deployment_key) BETWEEN 1 AND 64 AND
                       deployment_key ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
            display_name TEXT NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 120),
            provider_kind TEXT NOT NULL CHECK (provider_kind='BAILIAN_COMPATIBLE_MODE'),
            provider_model_id TEXT NOT NULL
                CHECK (length(provider_model_id) BETWEEN 1 AND 128 AND
                       provider_model_id ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'),
            connection_ref TEXT CHECK (length(connection_ref) BETWEEN 18 AND 128 AND
                       connection_ref ~ '^model-connection:[a-z][a-z0-9]*([._-][a-z0-9]+)*$'),
            declared_capabilities TEXT[] NOT NULL CHECK (
                declared_capabilities <@ ARRAY['chat','coding','search','thinking']::TEXT[] AND
                cardinality(declared_capabilities) <= 4 AND
                array_position(declared_capabilities, NULL) IS NULL),
            declared_context_window INTEGER CHECK (declared_context_window > 0),
            declared_max_output_tokens INTEGER CHECK (declared_max_output_tokens > 0),
            description TEXT CHECK (length(btrim(description)) BETWEEN 1 AND 2000),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            state TEXT NOT NULL CHECK (state IN ('DRAFT','ARCHIVED')),
            created_by TEXT NOT NULL CHECK (length(created_by) > 0),
            created_at TIMESTAMPTZ NOT NULL,
            updated_by TEXT NOT NULL CHECK (length(updated_by) > 0),
            updated_at TIMESTAMPTZ NOT NULL CHECK (updated_at >= created_at),
            archived_by TEXT,
            archived_at TIMESTAMPTZ,
            archive_reason TEXT,
            CHECK (
                (state='DRAFT' AND archived_by IS NULL AND archived_at IS NULL
                 AND archive_reason IS NULL)
                OR
                (state='ARCHIVED' AND archived_by IS NOT NULL AND length(archived_by) > 0
                 AND archived_at IS NOT NULL AND archived_at=updated_at
                 AND archive_reason IS NOT NULL AND length(btrim(archive_reason)) BETWEEN 1 AND 500)
            )
        )
    """)
    op.execute("""
        CREATE FUNCTION model_gateway.guard_deployment_update() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF OLD.state='ARCHIVED' OR NEW.revision <> OLD.revision + 1
               OR (NEW.id,NEW.deployment_key,NEW.created_by,NEW.created_at)
                   IS DISTINCT FROM (OLD.id,OLD.deployment_key,OLD.created_by,OLD.created_at)
               OR NEW.updated_at < OLD.updated_at
            THEN RAISE EXCEPTION 'invalid model deployment transition'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER deployment_update_guard BEFORE UPDATE ON model_gateway.deployment
        FOR EACH ROW EXECUTE FUNCTION model_gateway.guard_deployment_update()
    """)
    op.execute(
        """
        CREATE TABLE model_gateway.idempotency_record (
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
            CONSTRAINT uq_model_gateway_idempotency_scope
                UNIQUE (actor, operation, idempotency_key),
            CONSTRAINT ck_model_gateway_idempotency_actor CHECK (length(actor) > 0),
            CONSTRAINT ck_model_gateway_idempotency_operation CHECK (length(operation) > 0),
            CONSTRAINT ck_model_gateway_idempotency_key CHECK (length(idempotency_key) > 0),
            CONSTRAINT ck_model_gateway_idempotency_fingerprint
                CHECK (length(request_fingerprint) > 0),
            CONSTRAINT ck_model_gateway_idempotency_state
                CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
            CONSTRAINT ck_model_gateway_idempotency_result
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
                        AND result_metadata IS NOT NULL
                        AND sealed_response IS NOT NULL
                        AND completed_at IS NOT NULL
                    )
                ),
            CONSTRAINT ck_model_gateway_idempotency_timestamps
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

    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='model_gateway_rw') THEN
                CREATE ROLE model_gateway_rw NOLOGIN;
            END IF;
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA model_gateway TO model_gateway_rw")
    op.execute("GRANT SELECT, INSERT ON model_gateway.deployment TO model_gateway_rw")
    op.execute("""
        GRANT UPDATE (display_name, provider_kind, provider_model_id, connection_ref,
            declared_capabilities, declared_context_window, declared_max_output_tokens,
            description, state, revision, updated_by, updated_at, archived_by, archived_at,
            archive_reason) ON model_gateway.deployment TO model_gateway_rw
    """)
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON model_gateway.idempotency_record TO model_gateway_rw"
    )
    op.execute("REVOKE ALL ON FUNCTION model_gateway.guard_deployment_update() FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TABLE model_gateway.idempotency_record")
    op.execute("DROP TABLE model_gateway.deployment")
    op.execute("DROP FUNCTION model_gateway.guard_deployment_update()")
    op.execute("REVOKE USAGE ON SCHEMA model_gateway FROM model_gateway_rw")
    op.execute("DROP SCHEMA model_gateway")
