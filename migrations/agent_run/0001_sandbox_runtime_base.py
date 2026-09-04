"""V0.9 provider-neutral Sandbox Environment, Materialization and Lease ledger.

The migration creates only a NOLOGIN privilege role. Infrastructure owns the
runtime login and credential lifecycle.
"""

from alembic import op

revision = "0001_agent_run_sandbox"
down_revision = None
branch_labels = ("agent_run",)
depends_on = "0007_audit_query_request_id"

_AUDIT_SIGNATURE = (
    "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS agent_run")
    op.execute(
        """
        CREATE TABLE agent_run.sandbox_environment (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            requirement_id TEXT NOT NULL,
            trust_tier TEXT NOT NULL,
            state TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_agent_run_environment_requirement
                UNIQUE (workspace_id, requirement_id),
            CONSTRAINT ck_agent_run_environment_refs CHECK (
                length(btrim(workspace_id)) > 0
                AND length(btrim(requirement_id)) > 0
            ),
            CONSTRAINT ck_agent_run_environment_trust CHECK (trust_tier = 'LAB_ONLY'),
            CONSTRAINT ck_agent_run_environment_state CHECK (state IN ('ACTIVE', 'DISABLED')),
            CONSTRAINT ck_agent_run_environment_revision CHECK (revision >= 1),
            CONSTRAINT ck_agent_run_environment_timestamps CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_run.capacity_ledger (
            environment_id UUID PRIMARY KEY,
            policy_version TEXT NOT NULL,
            policy_enabled BOOLEAN NOT NULL,
            active_attempt_limit INTEGER NOT NULL,
            maximum_units INTEGER NOT NULL,
            active_attempts INTEGER NOT NULL,
            active_units INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_agent_run_capacity_environment
                FOREIGN KEY (environment_id)
                REFERENCES agent_run.sandbox_environment(id),
            CONSTRAINT ck_agent_run_capacity_policy CHECK (
                length(btrim(policy_version)) > 0
                AND active_attempt_limit >= 1
                AND maximum_units >= 1
            ),
            CONSTRAINT ck_agent_run_capacity_usage CHECK (
                active_attempts >= 0
                AND active_attempts <= active_attempt_limit
                AND active_units >= 0
                AND active_units <= maximum_units
            ),
            CONSTRAINT ck_agent_run_capacity_revision CHECK (revision >= 1),
            CONSTRAINT ck_agent_run_capacity_timestamps CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_run.sandbox_materialization (
            id UUID PRIMARY KEY,
            environment_id UUID NOT NULL,
            execution_id TEXT NOT NULL,
            execution_kind TEXT NOT NULL,
            binding_digest TEXT NOT NULL,
            deadline_at TIMESTAMPTZ NOT NULL,
            state TEXT NOT NULL,
            revision INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            resource_unit_weight INTEGER NOT NULL,
            runtime_profile JSONB NOT NULL,
            resource_profile JSONB NOT NULL,
            runner_manifest JSONB NOT NULL,
            boundary_manifest JSONB NOT NULL,
            policy_versions JSONB NOT NULL,
            denial_code TEXT,
            failure_dimension TEXT,
            evidence_persisted_at TIMESTAMPTZ,
            fenced_at TIMESTAMPTZ,
            secret_revoked_at TIMESTAMPTZ,
            lease_released_at TIMESTAMPTZ,
            destroyed_at TIMESTAMPTZ,
            terminal_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_agent_run_materialization_environment
                FOREIGN KEY (environment_id)
                REFERENCES agent_run.sandbox_environment(id),
            CONSTRAINT uq_agent_run_materialization_execution_generation
                UNIQUE (execution_id, generation),
            CONSTRAINT uq_agent_run_materialization_subject
                UNIQUE (id, execution_id, generation),
            CONSTRAINT ck_agent_run_materialization_refs CHECK (
                length(btrim(execution_id)) > 0
                AND binding_digest ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_agent_run_materialization_kind
                CHECK (execution_kind = 'SINGLE_REPOSITORY_FIX'),
            CONSTRAINT ck_agent_run_materialization_state CHECK (
                state IN (
                    'PROVISIONING', 'READY', 'RELEASING', 'RELEASED',
                    'FINALIZING', 'FINALIZED', 'CANCELING', 'CANCELED',
                    'TIMED_OUT', 'FAILED', 'QUARANTINED'
                )
            ),
            CONSTRAINT ck_agent_run_materialization_versions CHECK (
                revision >= 1 AND generation >= 1 AND resource_unit_weight >= 1
            ),
            CONSTRAINT ck_agent_run_materialization_snapshots CHECK (
                jsonb_typeof(runtime_profile) = 'object'
                AND jsonb_typeof(resource_profile) = 'object'
                AND jsonb_typeof(runner_manifest) = 'object'
                AND jsonb_typeof(boundary_manifest) = 'object'
                AND jsonb_typeof(policy_versions) = 'array'
            ),
            CONSTRAINT ck_agent_run_materialization_denial CHECK (
                denial_code IS NULL
                OR denial_code IN (
                    'CAPACITY_UNAVAILABLE', 'POLICY_LIMIT_REACHED', 'POLICY_DISABLED',
                    'RUNTIME_BINDING_INVALID', 'RUNTIME_CAPABILITY_DENIED',
                    'RUNTIME_BOUNDARY_VIOLATION', 'STALE_RUNNER_GENERATION',
                    'RESOURCE_EXHAUSTED'
                )
            ),
            CONSTRAINT ck_agent_run_materialization_cleanup_order CHECK (
                (fenced_at IS NULL OR evidence_persisted_at IS NOT NULL)
                AND (secret_revoked_at IS NULL OR fenced_at IS NOT NULL)
                AND (lease_released_at IS NULL OR secret_revoked_at IS NOT NULL)
                AND (destroyed_at IS NULL OR lease_released_at IS NOT NULL)
                AND (
                    evidence_persisted_at IS NULL OR fenced_at IS NULL
                    OR fenced_at >= evidence_persisted_at
                )
                AND (
                    fenced_at IS NULL OR secret_revoked_at IS NULL
                    OR secret_revoked_at >= fenced_at
                )
                AND (
                    secret_revoked_at IS NULL OR lease_released_at IS NULL
                    OR lease_released_at >= secret_revoked_at
                )
                AND (
                    lease_released_at IS NULL OR destroyed_at IS NULL
                    OR destroyed_at >= lease_released_at
                )
            ),
            CONSTRAINT ck_agent_run_materialization_terminal CHECK (
                (
                    state IN ('RELEASED', 'FINALIZED', 'CANCELED', 'TIMED_OUT', 'FAILED')
                    AND destroyed_at IS NOT NULL
                    AND terminal_at IS NOT NULL
                )
                OR (
                    state NOT IN ('RELEASED', 'FINALIZED', 'CANCELED', 'TIMED_OUT', 'FAILED')
                    AND terminal_at IS NULL
                )
            ),
            CONSTRAINT ck_agent_run_materialization_timestamps CHECK (
                updated_at >= created_at
                AND (terminal_at IS NULL OR terminal_at BETWEEN created_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_run_active_materialization "
        "ON agent_run.sandbox_materialization (execution_id) "
        "WHERE state IN ("
        "'PROVISIONING', 'READY', 'RELEASING', 'FINALIZING', 'CANCELING', 'QUARANTINED'"
        ")"
    )
    op.execute(
        "CREATE INDEX ix_agent_run_materialization_environment_state "
        "ON agent_run.sandbox_materialization (environment_id, state, created_at, id)"
    )
    op.execute(
        """
        CREATE TABLE agent_run.capacity_lease (
            id UUID PRIMARY KEY,
            environment_id UUID NOT NULL,
            materialization_id UUID NOT NULL,
            execution_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            unit_weight INTEGER NOT NULL,
            state TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            revision INTEGER NOT NULL,
            acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            released_at TIMESTAMPTZ,
            quarantined_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_agent_run_lease_environment
                FOREIGN KEY (environment_id)
                REFERENCES agent_run.sandbox_environment(id),
            CONSTRAINT fk_agent_run_lease_materialization FOREIGN KEY (
                materialization_id, execution_id, generation
            ) REFERENCES agent_run.sandbox_materialization (
                id, execution_id, generation
            ),
            CONSTRAINT uq_agent_run_lease_materialization UNIQUE (materialization_id),
            CONSTRAINT ck_agent_run_lease_values CHECK (
                length(btrim(execution_id)) > 0
                AND generation >= 1
                AND unit_weight >= 1
                AND revision >= 1
                AND expires_at > acquired_at
            ),
            CONSTRAINT ck_agent_run_lease_state CHECK (
                (state = 'ACTIVE' AND released_at IS NULL AND quarantined_at IS NULL)
                OR (state = 'RELEASED' AND released_at IS NOT NULL AND quarantined_at IS NULL)
                OR (state = 'QUARANTINED' AND quarantined_at IS NOT NULL)
            ),
            CONSTRAINT ck_agent_run_lease_timestamps CHECK (
                updated_at >= acquired_at
                AND (released_at IS NULL OR released_at BETWEEN acquired_at AND updated_at)
                AND (
                    quarantined_at IS NULL
                    OR quarantined_at BETWEEN acquired_at AND updated_at
                )
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_run_active_lease_execution "
        "ON agent_run.capacity_lease (execution_id) WHERE state = 'ACTIVE'"
    )
    op.execute(
        "CREATE INDEX ix_agent_run_lease_reconcile "
        "ON agent_run.capacity_lease (expires_at, id) "
        "WHERE state IN ('ACTIVE', 'QUARANTINED')"
    )
    op.execute(
        """
        CREATE TABLE agent_run.runner_generation (
            id UUID PRIMARY KEY,
            materialization_id UUID NOT NULL,
            execution_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            fencing_token_digest TEXT NOT NULL,
            binding_digest TEXT NOT NULL,
            protocol_version TEXT NOT NULL,
            deadline_at TIMESTAMPTZ NOT NULL,
            state TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ready_at TIMESTAMPTZ,
            fenced_at TIMESTAMPTZ,
            released_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_agent_run_generation_materialization FOREIGN KEY (
                materialization_id, execution_id, generation
            ) REFERENCES agent_run.sandbox_materialization (
                id, execution_id, generation
            ),
            CONSTRAINT uq_agent_run_generation_execution UNIQUE (execution_id, generation),
            CONSTRAINT uq_agent_run_generation_materialization UNIQUE (materialization_id),
            CONSTRAINT ck_agent_run_generation_refs CHECK (
                length(btrim(execution_id)) > 0
                AND fencing_token_digest ~ '^[0-9a-f]{64}$'
                AND binding_digest ~ '^sha256:[0-9a-f]{64}$'
                AND length(btrim(protocol_version)) > 0
            ),
            CONSTRAINT ck_agent_run_generation_values CHECK (
                generation >= 1 AND revision >= 1
            ),
            CONSTRAINT ck_agent_run_generation_state CHECK (
                (state = 'ACTIVE' AND fenced_at IS NULL AND released_at IS NULL)
                OR (state = 'FENCED' AND fenced_at IS NOT NULL AND released_at IS NULL)
                OR (state = 'RELEASED' AND fenced_at IS NOT NULL AND released_at IS NOT NULL)
                OR (state = 'QUARANTINED' AND fenced_at IS NOT NULL)
            ),
            CONSTRAINT ck_agent_run_generation_timestamps CHECK (
                updated_at >= created_at
                AND (ready_at IS NULL OR ready_at BETWEEN created_at AND updated_at)
                AND (fenced_at IS NULL OR fenced_at BETWEEN created_at AND updated_at)
                AND (released_at IS NULL OR released_at BETWEEN fenced_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_run_active_generation_execution "
        "ON agent_run.runner_generation (execution_id) WHERE state = 'ACTIVE'"
    )
    op.execute(
        """
        CREATE TABLE agent_run.evidence_reference (
            id UUID PRIMARY KEY,
            materialization_id UUID NOT NULL,
            sequence INTEGER NOT NULL,
            kind TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            artifact_version TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            classification TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_agent_run_evidence_materialization
                FOREIGN KEY (materialization_id)
                REFERENCES agent_run.sandbox_materialization(id),
            CONSTRAINT uq_agent_run_evidence_sequence UNIQUE (materialization_id, sequence),
            CONSTRAINT uq_agent_run_evidence_artifact
                UNIQUE (materialization_id, artifact_id, artifact_version, content_sha256),
            CONSTRAINT ck_agent_run_evidence_values CHECK (
                sequence >= 1
                AND kind IN (
                    'CHECKPOINT', 'PATCH', 'LOG', 'TEST_RESULT',
                    'PREVIEW_METADATA', 'DIAGNOSTIC'
                )
                AND length(btrim(artifact_id)) > 0
                AND length(btrim(artifact_version)) > 0
                AND content_sha256 ~ '^sha256:[0-9a-f]{64}$'
                AND length(btrim(classification)) > 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_run.command_receipt (
            id UUID PRIMARY KEY,
            actor TEXT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            subject_id UUID,
            state TEXT NOT NULL,
            http_status INTEGER,
            result_metadata JSONB,
            sealed_response BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT fk_agent_run_receipt_subject
                FOREIGN KEY (subject_id)
                REFERENCES agent_run.sandbox_materialization(id),
            CONSTRAINT uq_agent_run_command_scope
                UNIQUE (actor, operation, idempotency_key),
            CONSTRAINT ck_agent_run_command_values CHECK (
                length(btrim(actor)) > 0
                AND length(btrim(operation)) > 0
                AND length(btrim(idempotency_key)) BETWEEN 8 AND 128
                AND request_fingerprint ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_agent_run_command_state CHECK (
                (
                    state = 'IN_PROGRESS'
                    AND http_status IS NULL
                    AND result_metadata IS NULL
                    AND sealed_response IS NULL
                    AND completed_at IS NULL
                )
                OR (
                    state = 'COMPLETED'
                    AND http_status BETWEEN 100 AND 599
                    AND jsonb_typeof(result_metadata) = 'object'
                    AND sealed_response IS NOT NULL
                    AND completed_at IS NOT NULL
                )
            ),
            CONSTRAINT ck_agent_run_command_timestamps CHECK (
                updated_at >= created_at
                AND (completed_at IS NULL OR completed_at BETWEEN created_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_run.reconciliation_run (
            id UUID PRIMARY KEY,
            environment_id UUID NOT NULL,
            execution_id TEXT,
            actor TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            state TEXT NOT NULL,
            scanned_count INTEGER NOT NULL,
            reconciled_count INTEGER NOT NULL,
            denial_code TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_agent_run_reconciliation_environment
                FOREIGN KEY (environment_id)
                REFERENCES agent_run.sandbox_environment(id),
            CONSTRAINT ck_agent_run_reconciliation_refs CHECK (
                (execution_id IS NULL OR length(btrim(execution_id)) > 0)
                AND length(btrim(actor)) > 0
                AND length(btrim(correlation_id)) > 0
            ),
            CONSTRAINT ck_agent_run_reconciliation_counts CHECK (
                scanned_count >= 0
                AND reconciled_count >= 0
                AND reconciled_count <= scanned_count
            ),
            CONSTRAINT ck_agent_run_reconciliation_state CHECK (
                (state = 'RUNNING' AND completed_at IS NULL)
                OR (state IN ('COMPLETED', 'FAILED') AND completed_at IS NOT NULL)
            ),
            CONSTRAINT ck_agent_run_reconciliation_denial CHECK (
                denial_code IS NULL
                OR denial_code IN (
                    'CAPACITY_UNAVAILABLE', 'POLICY_LIMIT_REACHED', 'POLICY_DISABLED',
                    'RUNTIME_BINDING_INVALID', 'RUNTIME_CAPABILITY_DENIED',
                    'RUNTIME_BOUNDARY_VIOLATION', 'STALE_RUNNER_GENERATION',
                    'RESOURCE_EXHAUSTED'
                )
            ),
            CONSTRAINT ck_agent_run_reconciliation_timestamps CHECK (
                updated_at >= created_at
                AND (completed_at IS NULL OR completed_at BETWEEN created_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_agent_run_reconciliation_scope "
        "ON agent_run.reconciliation_run (environment_id, observed_at, id)"
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_run_rw') THEN
                CREATE ROLE agent_run_rw NOLOGIN;
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA agent_run TO agent_run_rw")
    op.execute("GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA agent_run TO agent_run_rw")
    op.execute(
        "GRANT UPDATE (state, revision, updated_at) "
        "ON agent_run.sandbox_environment TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (policy_version, policy_enabled, active_attempt_limit, "
        "maximum_units, active_attempts, active_units, revision, updated_at) "
        "ON agent_run.capacity_ledger TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (state, revision, denial_code, failure_dimension, "
        "evidence_persisted_at, fenced_at, secret_revoked_at, lease_released_at, "
        "destroyed_at, terminal_at, updated_at) "
        "ON agent_run.sandbox_materialization TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (state, revision, released_at, quarantined_at, updated_at) "
        "ON agent_run.capacity_lease TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (state, revision, ready_at, fenced_at, released_at, updated_at) "
        "ON agent_run.runner_generation TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (subject_id, state, http_status, result_metadata, sealed_response, "
        "updated_at, completed_at) ON agent_run.command_receipt TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (state, scanned_count, reconciled_count, denial_code, "
        "completed_at, updated_at) ON agent_run.reconciliation_run TO agent_run_rw"
    )
    op.execute("GRANT USAGE ON SCHEMA audit TO agent_run_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_AUDIT_SIGNATURE} TO agent_run_rw")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            business_rows BIGINT;
        BEGIN
            SELECT
                (SELECT count(*) FROM agent_run.sandbox_environment)
                + (SELECT count(*) FROM agent_run.capacity_ledger)
                + (SELECT count(*) FROM agent_run.sandbox_materialization)
                + (SELECT count(*) FROM agent_run.capacity_lease)
                + (SELECT count(*) FROM agent_run.runner_generation)
                + (SELECT count(*) FROM agent_run.evidence_reference)
                + (SELECT count(*) FROM agent_run.command_receipt)
                + (SELECT count(*) FROM agent_run.reconciliation_run)
            INTO business_rows;
            IF business_rows > 0 THEN
                RAISE EXCEPTION 'refusing Agent Run downgrade: business rows still exist';
            END IF;
        END $$
        """
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_AUDIT_SIGNATURE} FROM agent_run_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM agent_run_rw")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA agent_run FROM agent_run_rw")
    op.execute("REVOKE USAGE ON SCHEMA agent_run FROM agent_run_rw")
    op.execute("DROP TABLE agent_run.reconciliation_run")
    op.execute("DROP TABLE agent_run.command_receipt")
    op.execute("DROP TABLE agent_run.evidence_reference")
    op.execute("DROP TABLE agent_run.runner_generation")
    op.execute("DROP TABLE agent_run.capacity_lease")
    op.execute("DROP TABLE agent_run.sandbox_materialization")
    op.execute("DROP TABLE agent_run.capacity_ledger")
    op.execute("DROP TABLE agent_run.sandbox_environment")
    op.execute("DROP SCHEMA agent_run")
