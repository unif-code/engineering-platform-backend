"""Immutable Agent Control Plane durable facts.

This branch has no cross-schema foreign keys.  Runtime identity, Requirement,
Artifact, and orchestration references remain immutable platform identifiers.
"""

from alembic import op

revision = "0001_agent_control_plane"
down_revision = None
branch_labels = ("agent",)
depends_on = "0002_audit_transactional_append"

_AUDIT_APPEND_SIGNATURE = (
    "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"
)
_SEED_NAME = "agent/dev-control-plane-probe"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS agent")
    op.execute(
        """
        CREATE TABLE agent.agent_definition (
            id UUID NOT NULL,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            capability_declarations JSONB NOT NULL,
            skill_declarations JSONB NOT NULL,
            runtime_permissions JSONB NOT NULL,
            input_schema JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_agent_definition PRIMARY KEY (id, version),
            CONSTRAINT uq_agent_definition_name_version UNIQUE (name, version),
            CONSTRAINT ck_agent_definition_version CHECK (version >= 1),
            CONSTRAINT ck_agent_definition_name CHECK (length(btrim(name)) > 0),
            CONSTRAINT ck_agent_definition_capabilities
                CHECK (jsonb_typeof(capability_declarations) = 'array'),
            CONSTRAINT ck_agent_definition_skills
                CHECK (jsonb_typeof(skill_declarations) = 'array'),
            CONSTRAINT ck_agent_definition_permissions
                CHECK (jsonb_typeof(runtime_permissions) = 'array'),
            CONSTRAINT ck_agent_definition_input_schema
                CHECK (jsonb_typeof(input_schema) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent.agent_run (
            id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL,
            goal_ref TEXT NOT NULL,
            created_by TEXT NOT NULL,
            definition_id UUID NOT NULL,
            definition_version INTEGER NOT NULL,
            latest_attempt_id UUID NOT NULL,
            state TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_agent_run_refs CHECK (
                length(btrim(goal_ref)) > 0 AND length(btrim(created_by)) > 0
            ),
            CONSTRAINT ck_agent_run_definition_version CHECK (definition_version >= 1),
            CONSTRAINT ck_agent_run_state CHECK (
                state IN ('ACTIVE', 'SUCCEEDED', 'FAILED', 'CANCELED', 'TIMED_OUT')
            ),
            CONSTRAINT ck_agent_run_revision CHECK (revision >= 1),
            CONSTRAINT ck_agent_run_timestamps CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent.agent_attempt (
            id UUID PRIMARY KEY,
            run_id UUID NOT NULL,
            number INTEGER NOT NULL,
            state TEXT NOT NULL,
            binding_id UUID NOT NULL,
            binding_digest TEXT NOT NULL,
            runner_generation INTEGER NOT NULL,
            fencing_token TEXT NOT NULL,
            checkpoint_id UUID,
            event_sequence INTEGER NOT NULL DEFAULT 0,
            waiting_deadline TIMESTAMPTZ,
            terminal_evidence JSONB,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_agent_attempt_run_number UNIQUE (run_id, number),
            CONSTRAINT ck_agent_attempt_number CHECK (number >= 1),
            CONSTRAINT ck_agent_attempt_state CHECK (state IN (
                'CREATED', 'BINDING', 'QUEUED', 'PROVISIONING', 'RUNNING',
                'WAITING_INPUT', 'FINALIZING', 'SUCCEEDED', 'FAILED',
                'CANCELING', 'CANCELED', 'TIMED_OUT'
            )),
            CONSTRAINT ck_agent_attempt_binding_digest CHECK (binding_digest ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_agent_attempt_generation CHECK (runner_generation >= 1),
            CONSTRAINT ck_agent_attempt_fence CHECK (length(btrim(fencing_token)) > 0),
            CONSTRAINT ck_agent_attempt_event_sequence CHECK (event_sequence >= 0),
            CONSTRAINT ck_agent_attempt_terminal_evidence CHECK (
                terminal_evidence IS NULL OR jsonb_typeof(terminal_evidence) = 'object'
            ),
            CONSTRAINT ck_agent_attempt_revision CHECK (revision >= 1),
            CONSTRAINT ck_agent_attempt_timestamps CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent.execution_binding (
            id UUID PRIMARY KEY,
            attempt_id UUID NOT NULL UNIQUE,
            source TEXT NOT NULL,
            digest TEXT NOT NULL,
            snapshot JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_agent_binding_source CHECK (source IN ('DEV_FAKE', 'CONFIGURATION')),
            CONSTRAINT ck_agent_binding_digest CHECK (digest ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_agent_binding_snapshot CHECK (jsonb_typeof(snapshot) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent.canonical_event (
            event_id UUID PRIMARY KEY,
            event_type TEXT NOT NULL,
            attempt_id UUID NOT NULL,
            runner_generation INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            correlation_id TEXT NOT NULL,
            causation_id TEXT,
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            data JSONB NOT NULL,
            payload_digest TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_agent_canonical_event_id UNIQUE (event_id),
            CONSTRAINT uq_agent_canonical_event_sequence
                UNIQUE (attempt_id, runner_generation, sequence),
            CONSTRAINT ck_agent_event_type CHECK (event_type IN (
                'ATTEMPT_QUEUED', 'ATTEMPT_PROVISIONING', 'ATTEMPT_RUNNING',
                'WAITING_INPUT', 'ATTEMPT_FINALIZING', 'ATTEMPT_SUCCEEDED',
                'ATTEMPT_FAILED', 'ATTEMPT_CANCELED', 'ATTEMPT_TIMED_OUT'
            )),
            CONSTRAINT ck_agent_event_generation CHECK (runner_generation >= 1),
            CONSTRAINT ck_agent_event_sequence CHECK (sequence >= 1),
            CONSTRAINT ck_agent_event_trace CHECK (
                length(btrim(correlation_id)) > 0 AND length(btrim(trace_id)) > 0
                AND length(btrim(span_id)) > 0 AND length(btrim(summary)) > 0
            ),
            CONSTRAINT ck_agent_event_data CHECK (jsonb_typeof(data) = 'object'),
            CONSTRAINT ck_agent_event_digest CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent.checkpoint (
            id UUID PRIMARY KEY,
            attempt_id UUID NOT NULL,
            artifact_id TEXT NOT NULL,
            artifact_version TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            classification TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_agent_checkpoint_refs CHECK (
                length(btrim(artifact_id)) > 0 AND length(btrim(artifact_version)) > 0
                AND length(btrim(schema_version)) > 0 AND length(btrim(adapter_version)) > 0
            ),
            CONSTRAINT ck_agent_checkpoint_digest CHECK (content_sha256 ~ '^sha256:[0-9a-f]{64}$'),
            CONSTRAINT ck_agent_checkpoint_classification
                CHECK (classification IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent.workflow_command (
            id UUID PRIMARY KEY,
            command_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            attempt_id UUID NOT NULL,
            generation INTEGER NOT NULL,
            state TEXT NOT NULL,
            dispatch_attempts INTEGER NOT NULL DEFAULT 0,
            receipt JSONB,
            last_error_code TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            dispatched_at TIMESTAMPTZ,
            CONSTRAINT ck_agent_command_key CHECK (length(btrim(command_key)) > 0),
            CONSTRAINT ck_agent_command_kind CHECK (kind IN ('START', 'CANCEL', 'RESUME')),
            CONSTRAINT ck_agent_command_generation CHECK (generation >= 1),
            CONSTRAINT ck_agent_command_state
                CHECK (state IN ('PLANNED', 'DISPATCHED', 'UNKNOWN', 'FAILED')),
            CONSTRAINT ck_agent_command_attempts CHECK (dispatch_attempts >= 0),
            CONSTRAINT ck_agent_command_receipt CHECK (
                receipt IS NULL OR jsonb_typeof(receipt) = 'object'
            ),
            CONSTRAINT ck_agent_command_timestamps CHECK (
                updated_at >= created_at AND (dispatched_at IS NULL OR dispatched_at >= created_at)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent.idempotency_key (
            id UUID PRIMARY KEY,
            actor TEXT NOT NULL,
            operation TEXT NOT NULL,
            key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            http_status INTEGER,
            result_metadata JSONB,
            sealed_response BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT uq_agent_idempotency_scope UNIQUE (actor, operation, key),
            CONSTRAINT ck_agent_idempotency_values CHECK (
                length(btrim(actor)) > 0 AND length(btrim(operation)) > 0
                AND length(btrim(key)) > 0 AND length(btrim(request_fingerprint)) > 0
            ),
            CONSTRAINT ck_agent_idempotency_state CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
            CONSTRAINT ck_agent_idempotency_result CHECK (
                (state = 'IN_PROGRESS' AND http_status IS NULL AND result_metadata IS NULL
                    AND sealed_response IS NULL AND completed_at IS NULL)
                OR
                (state = 'COMPLETED' AND http_status BETWEEN 100 AND 599
                    AND result_metadata IS NOT NULL AND sealed_response IS NOT NULL
                    AND completed_at IS NOT NULL)
            ),
            CONSTRAINT ck_agent_idempotency_timestamps CHECK (
                updated_at >= created_at
                AND (completed_at IS NULL OR completed_at BETWEEN created_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_agent_run_workspace_cursor "
        "ON agent.agent_run (workspace_id, created_at, id)"
    )
    op.execute(
        "CREATE INDEX ix_agent_event_attempt_cursor "
        "ON agent.canonical_event (attempt_id, runner_generation, sequence)"
    )
    op.execute(
        "CREATE INDEX ix_agent_command_planned ON agent.workflow_command (created_at, id) "
        "WHERE state IN ('PLANNED', 'UNKNOWN')"
    )
    op.execute(
        """
        INSERT INTO agent.agent_definition (
            id, version, name, capability_declarations, skill_declarations,
            runtime_permissions, input_schema
        ) VALUES (
            '00000000-0000-0000-0000-000000000800', 1, 'agent/dev-control-plane-probe',
            '["agent.run.execute"]'::jsonb,
            '["writing-plans"]'::jsonb,
            '["checkpoint.write","context.read","event.emit"]'::jsonb,
            jsonb_build_object(
                'additionalProperties', false,
                'properties', jsonb_build_object('goal', jsonb_build_object('type', 'string')),
                'required', jsonb_build_array('goal'),
                'type', 'object'
            )
        )
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_rw') THEN
                CREATE ROLE agent_rw NOLOGIN;
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA agent TO agent_rw")
    op.execute(
        "GRANT SELECT, INSERT ON agent.agent_definition, agent.execution_binding, "
        "agent.canonical_event, agent.checkpoint TO agent_rw"
    )
    op.execute("GRANT SELECT, INSERT ON agent.agent_run, agent.agent_attempt TO agent_rw")
    op.execute("GRANT SELECT, INSERT ON agent.workflow_command, agent.idempotency_key TO agent_rw")
    op.execute(
        "GRANT UPDATE (latest_attempt_id, state, revision, updated_at) "
        "ON agent.agent_run TO agent_rw"
    )
    op.execute(
        "GRANT UPDATE (state, runner_generation, fencing_token, checkpoint_id, event_sequence, "
        "waiting_deadline, terminal_evidence, revision, updated_at) "
        "ON agent.agent_attempt TO agent_rw"
    )
    op.execute(
        "GRANT UPDATE (state, dispatch_attempts, receipt, last_error_code, "
        "updated_at, dispatched_at) "
        "ON agent.workflow_command TO agent_rw"
    )
    op.execute(
        "GRANT UPDATE (state, http_status, result_metadata, sealed_response, "
        "updated_at, completed_at) "
        "ON agent.idempotency_key TO agent_rw"
    )
    op.execute("GRANT USAGE ON SCHEMA audit TO agent_rw")
    op.execute(
        "GRANT EXECUTE ON FUNCTION audit.append_event("
        "text,timestamptz,text,text,text,text,text,text,text,text,integer) TO agent_rw"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            business_rows BIGINT;
        BEGIN
            SELECT
                (SELECT count(*) FROM agent.agent_definition
                    WHERE NOT (name = 'agent/dev-control-plane-probe' AND version = 1))
                + (SELECT count(*) FROM agent.agent_run)
                + (SELECT count(*) FROM agent.agent_attempt)
                + (SELECT count(*) FROM agent.execution_binding)
                + (SELECT count(*) FROM agent.canonical_event)
                + (SELECT count(*) FROM agent.checkpoint)
                + (SELECT count(*) FROM agent.workflow_command)
                + (SELECT count(*) FROM agent.idempotency_key)
            INTO business_rows;
            IF business_rows > 0 THEN
                RAISE EXCEPTION 'refusing Agent downgrade: business rows still exist';
            END IF;
        END $$
        """
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_AUDIT_APPEND_SIGNATURE} FROM agent_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM agent_rw")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA agent FROM agent_rw")
    op.execute("REVOKE USAGE ON SCHEMA agent FROM agent_rw")
    op.execute("DROP TABLE agent.idempotency_key")
    op.execute("DROP TABLE agent.workflow_command")
    op.execute("DROP TABLE agent.checkpoint")
    op.execute("DROP TABLE agent.canonical_event")
    op.execute("DROP TABLE agent.execution_binding")
    op.execute("DROP TABLE agent.agent_attempt")
    op.execute("DROP TABLE agent.agent_run")
    op.execute("DROP TABLE agent.agent_definition")
    op.execute("DROP SCHEMA agent")
