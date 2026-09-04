"""Add durable command ownership and restricted runtime recovery facts."""

from alembic import op

revision = "0003_agent_run_recovery"
down_revision = "0002_agent_run_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization "
        "ADD COLUMN recovery_capsule BYTEA, ADD COLUMN cancellation_reason TEXT"
    )
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization "
        "ADD CONSTRAINT ck_agent_run_materialization_cancellation_reason CHECK ("
        "cancellation_reason IS NULL OR cancellation_reason IN "
        "('CANCELED','TIMED_OUT','TERMINATED','SECURITY_VIOLATION'))"
    )
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization "
        "DROP CONSTRAINT ck_agent_run_materialization_cleanup_terminal"
    )
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization "
        "ADD CONSTRAINT ck_agent_run_materialization_cleanup_terminal CHECK ("
        "cleanup_terminal_state IS NULL OR cleanup_terminal_state IN "
        "('RELEASED','FINALIZED','CANCELED','TIMED_OUT','FAILED'))"
    )

    op.execute(
        "ALTER TABLE agent_run.command_receipt "
        "ADD COLUMN owner_id UUID, ADD COLUMN owner_expires_at TIMESTAMPTZ, "
        "ADD COLUMN phase TEXT, ADD COLUMN progress JSONB NOT NULL DEFAULT '{}'::jsonb "
        "CHECK (jsonb_typeof(progress)='object')"
    )
    op.execute(
        "UPDATE agent_run.command_receipt SET "
        "phase=CASE WHEN state='COMPLETED' THEN 'COMPLETED' ELSE 'CLAIMED' END, "
        "owner_id=CASE WHEN state='IN_PROGRESS' THEN id ELSE NULL END, "
        "owner_expires_at=CASE WHEN state='IN_PROGRESS' THEN updated_at ELSE NULL END"
    )
    op.execute("ALTER TABLE agent_run.command_receipt ALTER COLUMN phase SET NOT NULL")
    op.execute("ALTER TABLE agent_run.command_receipt DROP CONSTRAINT ck_agent_run_command_state")
    op.execute(
        "ALTER TABLE agent_run.command_receipt "
        "ADD CONSTRAINT ck_agent_run_command_state CHECK ("
        "(state='IN_PROGRESS' AND owner_id IS NOT NULL AND owner_expires_at IS NOT NULL "
        "AND length(btrim(phase)) > 0 AND http_status IS NULL "
        "AND result_metadata IS NULL AND sealed_response IS NULL AND completed_at IS NULL) OR "
        "(state='COMPLETED' AND owner_id IS NULL AND owner_expires_at IS NULL "
        "AND phase='COMPLETED' AND http_status BETWEEN 100 AND 599 "
        "AND jsonb_typeof(result_metadata)='object' AND sealed_response IS NOT NULL "
        "AND completed_at IS NOT NULL))"
    )

    op.execute(
        """
        CREATE TABLE agent_run.preview_intent (
            command_id UUID PRIMARY KEY,
            materialization_id UUID NOT NULL,
            generation INTEGER NOT NULL,
            metadata JSONB NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            state TEXT NOT NULL,
            result_capsule BYTEA,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            CONSTRAINT fk_agent_run_preview_command FOREIGN KEY (command_id)
                REFERENCES agent_run.command_receipt(id),
            CONSTRAINT fk_agent_run_preview_materialization FOREIGN KEY (materialization_id)
                REFERENCES agent_run.sandbox_materialization(id),
            CONSTRAINT ck_agent_run_preview_generation CHECK (generation >= 1),
            CONSTRAINT ck_agent_run_preview_metadata CHECK (jsonb_typeof(metadata)='object'),
            CONSTRAINT ck_agent_run_preview_state CHECK (
                (state='INTENT' AND result_capsule IS NULL AND completed_at IS NULL) OR
                (state='PUBLISHED' AND result_capsule IS NOT NULL AND completed_at IS NOT NULL)
            ),
            CONSTRAINT ck_agent_run_preview_timestamps CHECK (
                updated_at >= created_at AND expires_at > created_at
                AND (completed_at IS NULL OR completed_at BETWEEN created_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_run.runtime_state (
            materialization_id UUID PRIMARY KEY,
            operation_id UUID NOT NULL UNIQUE,
            generation INTEGER NOT NULL,
            fencing_token_digest TEXT NOT NULL,
            binding_digest TEXT NOT NULL,
            protocol_version TEXT NOT NULL,
            deadline_at TIMESTAMPTZ NOT NULL,
            preview_enabled BOOLEAN NOT NULL,
            evidence_persisted BOOLEAN NOT NULL,
            side_effects_fenced BOOLEAN NOT NULL,
            secret_revoked BOOLEAN NOT NULL,
            destroyed BOOLEAN NOT NULL,
            preview_active BOOLEAN NOT NULL,
            preview_operation_id UUID,
            preview_id TEXT,
            access_ref TEXT,
            preview_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT fk_agent_run_runtime_materialization FOREIGN KEY (materialization_id)
                REFERENCES agent_run.sandbox_materialization(id),
            CONSTRAINT ck_agent_run_runtime_generation CHECK (generation >= 1),
            CONSTRAINT ck_agent_run_runtime_digests CHECK (
                fencing_token_digest ~ '^[0-9a-f]{64}$'
                AND binding_digest ~ '^sha256:[0-9a-f]{64}$'
                AND length(btrim(protocol_version)) > 0
            ),
            CONSTRAINT ck_agent_run_runtime_order CHECK (
                (side_effects_fenced = false OR evidence_persisted = true)
                AND (secret_revoked = false OR side_effects_fenced = true)
                AND (destroyed = false OR secret_revoked = true)
                AND (preview_active = false OR (side_effects_fenced = false
                    AND destroyed = false AND preview_operation_id IS NOT NULL
                    AND preview_id IS NOT NULL AND access_ref IS NOT NULL
                    AND preview_expires_at IS NOT NULL))
            ),
            CONSTRAINT ck_agent_run_runtime_timestamps CHECK (updated_at >= created_at)
        )
        """
    )

    op.execute(
        "GRANT SELECT, INSERT ON agent_run.preview_intent, agent_run.runtime_state TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (state, result_capsule, updated_at, completed_at) "
        "ON agent_run.preview_intent TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (evidence_persisted, side_effects_fenced, secret_revoked, "
        "destroyed, preview_active, "
        "preview_operation_id, preview_id, access_ref, preview_expires_at, updated_at) "
        "ON agent_run.runtime_state TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (recovery_capsule, cancellation_reason) "
        "ON agent_run.sandbox_materialization TO agent_run_rw"
    )
    op.execute(
        "GRANT UPDATE (owner_id, owner_expires_at, phase, progress) "
        "ON agent_run.command_receipt TO agent_run_rw"
    )


def downgrade() -> None:
    op.execute("DROP TABLE agent_run.runtime_state")
    op.execute("DROP TABLE agent_run.preview_intent")
    op.execute("ALTER TABLE agent_run.command_receipt DROP CONSTRAINT ck_agent_run_command_state")
    op.execute("ALTER TABLE agent_run.command_receipt DROP COLUMN phase")
    op.execute("ALTER TABLE agent_run.command_receipt DROP COLUMN progress")
    op.execute("ALTER TABLE agent_run.command_receipt DROP COLUMN owner_expires_at")
    op.execute("ALTER TABLE agent_run.command_receipt DROP COLUMN owner_id")
    op.execute(
        "ALTER TABLE agent_run.command_receipt "
        "ADD CONSTRAINT ck_agent_run_command_state CHECK ("
        "(state='IN_PROGRESS' AND http_status IS NULL AND result_metadata IS NULL "
        "AND sealed_response IS NULL AND completed_at IS NULL) OR "
        "(state='COMPLETED' AND http_status BETWEEN 100 AND 599 "
        "AND jsonb_typeof(result_metadata)='object' AND sealed_response IS NOT NULL "
        "AND completed_at IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization "
        "DROP CONSTRAINT ck_agent_run_materialization_cleanup_terminal"
    )
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization "
        "ADD CONSTRAINT ck_agent_run_materialization_cleanup_terminal CHECK ("
        "cleanup_terminal_state IS NULL OR cleanup_terminal_state IN "
        "('RELEASED','FINALIZED','CANCELED','TIMED_OUT'))"
    )
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization "
        "DROP CONSTRAINT ck_agent_run_materialization_cancellation_reason"
    )
    op.execute("ALTER TABLE agent_run.sandbox_materialization DROP COLUMN cancellation_reason")
    op.execute("ALTER TABLE agent_run.sandbox_materialization DROP COLUMN recovery_capsule")
