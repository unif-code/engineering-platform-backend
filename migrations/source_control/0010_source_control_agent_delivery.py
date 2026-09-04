"""Add credential-brokered Agent delivery effect ledgers."""

from alembic import op

revision = "0010_sc_agent_delivery"
down_revision = "0006_sc_mr_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE source_control.agent_push_request (
            id UUID PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            attempt_id UUID NOT NULL,
            attempt_generation BIGINT NOT NULL,
            execution_binding_digest TEXT NOT NULL,
            requirement_id UUID NOT NULL,
            work_item_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            repository_id TEXT NOT NULL,
            branch_binding_id UUID NOT NULL,
            branch_name TEXT NOT NULL,
            expected_remote_head_sha TEXT NOT NULL,
            target_commit_sha TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            artifact_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            grant_digest TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'AUTHORIZED',
            attempts INTEGER NOT NULL DEFAULT 0,
            issued_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            next_reconcile_at TIMESTAMPTZ,
            consumed_at TIMESTAMPTZ,
            observed_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            remote_head_sha TEXT,
            last_error_code TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_sc_agent_push_repository
                FOREIGN KEY (repository_id)
                REFERENCES source_control.workspace_repository(id),
            CONSTRAINT fk_sc_agent_push_branch_binding
                FOREIGN KEY (branch_binding_id)
                REFERENCES source_control.repository_branch_binding(id),
            CONSTRAINT uq_sc_agent_push_idempotency
                UNIQUE (workspace_id, idempotency_key),
            CONSTRAINT uq_sc_agent_push_coordinate
                UNIQUE (
                    attempt_id,
                    repository_id,
                    branch_name,
                    target_commit_sha,
                    content_digest
                ),
            CONSTRAINT ck_sc_agent_push_refs CHECK (
                length(btrim(idempotency_key)) BETWEEN 1 AND 200
                AND length(btrim(correlation_id)) BETWEEN 1 AND 200
                AND length(btrim(branch_name)) BETWEEN 1 AND 255
            ),
            CONSTRAINT ck_sc_agent_push_hashes CHECK (
                request_fingerprint ~ '^sha256:[0-9a-f]{64}$'
                AND execution_binding_digest ~ '^sha256:[0-9a-f]{64}$'
                AND grant_digest ~ '^sha256:[0-9a-f]{64}$'
                AND content_digest ~ '^sha256:[0-9a-f]{64}$'
                AND expected_remote_head_sha ~ '^[0-9a-f]{40}$'
                AND target_commit_sha ~ '^[0-9a-f]{40}$'
                AND (
                    remote_head_sha IS NULL
                    OR remote_head_sha ~ '^[0-9a-f]{40}$'
                )
            ),
            CONSTRAINT ck_sc_agent_push_ttl CHECK (
                expires_at > issued_at
                AND expires_at <= issued_at + interval '5 minutes'
                AND updated_at >= issued_at
                AND (consumed_at IS NULL OR consumed_at BETWEEN issued_at AND expires_at)
                AND (observed_at IS NULL OR observed_at >= issued_at)
                AND (completed_at IS NULL OR completed_at >= issued_at)
            ),
            CONSTRAINT ck_sc_agent_push_state CHECK (
                state IN (
                    'AUTHORIZED',
                    'IN_FLIGHT',
                    'UNKNOWN',
                    'RECONCILIATION',
                    'SUCCEEDED',
                    'BLOCKED',
                    'FENCED'
                )
                AND attempts >= 0
            ),
            CONSTRAINT ck_sc_agent_push_state_shape CHECK (
                (
                    state = 'AUTHORIZED'
                    AND consumed_at IS NULL
                    AND observed_at IS NULL
                    AND completed_at IS NULL
                    AND remote_head_sha IS NULL
                    AND last_error_code IS NULL
                    AND next_reconcile_at IS NULL
                )
                OR (
                    state = 'IN_FLIGHT'
                    AND consumed_at IS NOT NULL
                    AND observed_at IS NULL
                    AND completed_at IS NULL
                    AND remote_head_sha IS NULL
                    AND last_error_code IS NULL
                    AND next_reconcile_at IS NOT NULL
                )
                OR (
                    state IN ('UNKNOWN', 'RECONCILIATION')
                    AND consumed_at IS NOT NULL
                    AND completed_at IS NULL
                    AND last_error_code IS NOT NULL
                    AND length(btrim(last_error_code)) > 0
                    AND next_reconcile_at IS NOT NULL
                    AND (
                        (observed_at IS NULL AND remote_head_sha IS NULL)
                        OR (observed_at IS NOT NULL AND remote_head_sha IS NOT NULL)
                    )
                )
                OR (
                    state = 'SUCCEEDED'
                    AND consumed_at IS NOT NULL
                    AND observed_at IS NOT NULL
                    AND completed_at IS NOT NULL
                    AND remote_head_sha = target_commit_sha
                    AND last_error_code IS NULL
                    AND next_reconcile_at IS NULL
                )
                OR (
                    state IN ('BLOCKED', 'FENCED')
                    AND completed_at IS NOT NULL
                    AND last_error_code IS NOT NULL
                    AND length(btrim(last_error_code)) > 0
                    AND next_reconcile_at IS NULL
                    AND (
                        (observed_at IS NULL AND remote_head_sha IS NULL)
                        OR (observed_at IS NOT NULL AND remote_head_sha IS NOT NULL)
                    )
                )
            ),
            CONSTRAINT ck_sc_agent_push_artifacts CHECK (
                jsonb_typeof(artifact_refs) = 'array'
                AND jsonb_array_length(artifact_refs) <= 32
                AND NOT jsonb_path_exists(
                    artifact_refs,
                    '$[*] ? (@.type() != "string" || '
                    '!(@ like_regex "^.{1,255}.?$" flag "s") || '
                    '!(@ like_regex "[^[:space:]]"))'
                )
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sc_agent_push_reconcile "
        "ON source_control.agent_push_request (next_reconcile_at, id) "
        "WHERE state IN ('IN_FLIGHT', 'UNKNOWN', 'RECONCILIATION')"
    )
    op.execute(
        """
        CREATE TABLE source_control.agent_delivery_fence (
            attempt_id UUID PRIMARY KEY,
            fenced_generation BIGINT NOT NULL,
            reason_code TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            revocation_state TEXT NOT NULL,
            revoke_attempts INTEGER NOT NULL DEFAULT 0,
            next_revoke_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_sc_agent_fence_generation CHECK (fenced_generation >= 1),
            CONSTRAINT ck_sc_agent_fence_refs CHECK (
                length(btrim(reason_code)) BETWEEN 1 AND 100
                AND length(btrim(correlation_id)) BETWEEN 1 AND 200
                AND updated_at >= created_at
            ),
            CONSTRAINT ck_sc_agent_fence_revocation CHECK (
                revoke_attempts >= 0
                AND (
                    (
                        revocation_state IN ('PENDING', 'UNKNOWN')
                        AND next_revoke_at IS NOT NULL
                    )
                    OR (
                        revocation_state = 'SUCCEEDED'
                        AND next_revoke_at IS NULL
                    )
                )
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sc_agent_fence_revoke "
        "ON source_control.agent_delivery_fence (next_revoke_at, attempt_id) "
        "WHERE revocation_state IN ('PENDING', 'UNKNOWN')"
    )
    op.execute(
        """
        CREATE TABLE source_control.agent_delivery_fact (
            id UUID PRIMARY KEY,
            push_request_id UUID NOT NULL,
            topic TEXT NOT NULL,
            payload JSONB NOT NULL,
            correlation_id TEXT NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT fk_sc_agent_fact_request
                FOREIGN KEY (push_request_id)
                REFERENCES source_control.agent_push_request(id),
            CONSTRAINT uq_sc_agent_fact_request UNIQUE (push_request_id),
            CONSTRAINT ck_sc_agent_fact_topic CHECK (
                topic IN (
                    'source-control.agent-push-confirmed.v1',
                    'source-control.agent-push-fenced.v1'
                )
            ),
            CONSTRAINT ck_sc_agent_fact_payload CHECK (
                jsonb_typeof(payload) = 'object'
                AND payload ->> 'executorType' = 'AGENT'
                AND length(btrim(correlation_id)) BETWEEN 1 AND 200
                AND NOT payload ?| ARRAY[
                    'grantToken',
                    'fencingToken',
                    'credential',
                    'secret',
                    'environment',
                    'command',
                    'path'
                ]
            )
        )
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON "
        "source_control.agent_push_request, "
        "source_control.agent_delivery_fence TO source_control_rw"
    )
    op.execute("GRANT SELECT, INSERT ON source_control.agent_delivery_fact TO source_control_rw")


def downgrade() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
            IF EXISTS (SELECT 1 FROM source_control.agent_delivery_fact)
                OR EXISTS (SELECT 1 FROM source_control.agent_delivery_fence)
                OR EXISTS (SELECT 1 FROM source_control.agent_push_request)
            THEN
                RAISE EXCEPTION
                    'agent delivery facts prevent Source Control downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "REVOKE ALL ON "
        "source_control.agent_delivery_fact, "
        "source_control.agent_delivery_fence, "
        "source_control.agent_push_request FROM source_control_rw"
    )
    op.execute("DROP TABLE source_control.agent_delivery_fact")
    op.execute("DROP TABLE source_control.agent_delivery_fence")
    op.execute("DROP TABLE source_control.agent_push_request")
