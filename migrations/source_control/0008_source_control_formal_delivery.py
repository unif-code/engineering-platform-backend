"""Add formal task-to-main MR delivery facts and Effect shapes."""

from alembic import op

revision = "0008_sc_formal_delivery"
down_revision = "0007_sc_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE source_control.source_control_effect "
        "DROP CONSTRAINT ck_source_control_effect_operation_shape"
    )
    op.execute(
        "UPDATE source_control.source_control_effect SET subject_key="
        "'integration-work-item:' || work_item_id::text || ':' || (payload ->> 'headSha'), "
        "effect_key='source-control:create-integration-mr:' || work_item_id::text || ':' || "
        "(payload ->> 'headSha') WHERE operation='CREATE_INTEGRATION_MR'"
    )
    op.execute(
        """
        ALTER TABLE source_control.source_control_effect
        ADD CONSTRAINT ck_source_control_effect_operation_shape CHECK (
            (
                operation = 'CREATE_TASK_BRANCH'
                AND subject_key = 'work-item:' || work_item_id::text
                AND work_item_number IS NOT NULL AND work_item_number >= 1
                AND branch_name IS NOT NULL AND length(btrim(branch_name)) > 0
                AND base_commit_sha IS NOT NULL AND length(btrim(base_commit_sha)) > 0
                AND payload = '{}'::jsonb
            )
            OR (
                operation = 'CREATE_INTEGRATION_MR'
                AND subject_key = 'integration-work-item:' || work_item_id::text || ':' ||
                    (payload ->> 'headSha')
                AND work_item_number IS NULL AND branch_name IS NULL
                AND base_commit_sha IS NULL
                AND payload = jsonb_build_object(
                    'branchBindingId', payload -> 'branchBindingId',
                    'headSha', payload -> 'headSha'
                )
                AND jsonb_typeof(payload -> 'branchBindingId') = 'string'
                AND length(btrim(payload ->> 'branchBindingId')) > 0
                AND (payload ->> 'headSha') ~ '^[0-9a-f]{40}$'
            )
            OR (
                operation = 'MERGE_INTEGRATION_MR'
                AND work_item_number IS NULL AND branch_name IS NULL
                AND base_commit_sha IS NULL
                AND payload = jsonb_build_object(
                    'bindingId', payload -> 'bindingId',
                    'requestedHeadSha', payload -> 'requestedHeadSha'
                )
                AND jsonb_typeof(payload -> 'bindingId') = 'string'
                AND length(btrim(payload ->> 'bindingId')) > 0
                AND (payload ->> 'requestedHeadSha') ~ '^[0-9a-f]{40}$'
                AND subject_key = 'mr:' || (payload ->> 'bindingId') || ':' ||
                    (payload ->> 'requestedHeadSha')
            )
            OR (
                operation = 'CREATE_FORMAL_MR'
                AND work_item_number IS NULL AND branch_name IS NULL
                AND base_commit_sha IS NULL
                AND payload = jsonb_build_object(
                    'acceptanceDecisionId', payload -> 'acceptanceDecisionId',
                    'branchBindingId', payload -> 'branchBindingId',
                    'headSha', payload -> 'headSha'
                )
                AND jsonb_typeof(payload -> 'acceptanceDecisionId') = 'string'
                AND length(btrim(payload ->> 'acceptanceDecisionId')) > 0
                AND jsonb_typeof(payload -> 'branchBindingId') = 'string'
                AND length(btrim(payload ->> 'branchBindingId')) > 0
                AND jsonb_typeof(payload -> 'headSha') = 'string'
                AND length(btrim(payload ->> 'headSha')) > 0
                AND (payload ->> 'headSha') ~ '^[0-9a-f]{40}$'
                AND subject_key = 'formal-work-item:' || work_item_id::text || ':' ||
                    (payload ->> 'headSha') || ':' || request_fingerprint
            )
            OR (
                operation = 'MERGE_FORMAL_MR'
                AND work_item_number IS NULL AND branch_name IS NULL
                AND base_commit_sha IS NULL
                AND payload = jsonb_build_object(
                    'acceptanceDecisionId', payload -> 'acceptanceDecisionId',
                    'bindingId', payload -> 'bindingId',
                    'requestedHeadSha', payload -> 'requestedHeadSha',
                    'reviewDecisionId', payload -> 'reviewDecisionId'
                )
                AND jsonb_typeof(payload -> 'acceptanceDecisionId') = 'string'
                AND length(btrim(payload ->> 'acceptanceDecisionId')) > 0
                AND jsonb_typeof(payload -> 'bindingId') = 'string'
                AND length(btrim(payload ->> 'bindingId')) > 0
                AND jsonb_typeof(payload -> 'requestedHeadSha') = 'string'
                AND length(btrim(payload ->> 'requestedHeadSha')) > 0
                AND (payload ->> 'requestedHeadSha') ~ '^[0-9a-f]{40}$'
                AND jsonb_typeof(payload -> 'reviewDecisionId') = 'string'
                AND length(btrim(payload ->> 'reviewDecisionId')) > 0
                AND subject_key = 'formal-mr:' || (payload ->> 'bindingId') || ':' ||
                    (payload ->> 'requestedHeadSha') || ':' || request_fingerprint
            )
        )
        """
    )

    op.execute(
        "ALTER TABLE source_control.merge_request_binding "
        "DROP CONSTRAINT uq_source_control_mr_binding_work_item, "
        "DROP CONSTRAINT uq_source_control_mr_binding_branch, "
        "DROP CONSTRAINT ck_source_control_mr_binding_kind, "
        "DROP CONSTRAINT ck_source_control_mr_binding_target"
    )
    op.execute(
        "ALTER TABLE source_control.source_control_effect "
        "ADD CONSTRAINT uq_sc_effect_id_operation UNIQUE (id, operation)"
    )
    op.execute(
        "ALTER TABLE source_control.merge_request_binding "
        "DROP CONSTRAINT fk_source_control_mr_binding_effect, "
        "ADD COLUMN create_effect_operation TEXT GENERATED ALWAYS AS ("
        "CASE kind "
        "WHEN 'INTEGRATION' THEN 'CREATE_INTEGRATION_MR' "
        "WHEN 'FORMAL' THEN 'CREATE_FORMAL_MR' "
        "END) STORED"
    )
    op.execute(
        "ALTER TABLE source_control.merge_request_binding "
        "ADD COLUMN superseded_at TIMESTAMPTZ, "
        "ALTER COLUMN create_effect_operation SET NOT NULL, "
        "ADD CONSTRAINT fk_source_control_mr_binding_effect FOREIGN KEY ("
        "create_effect_id, create_effect_operation) REFERENCES "
        "source_control.source_control_effect (id, operation), "
        "ADD CONSTRAINT ck_source_control_mr_binding_kind CHECK ("
        "kind IN ('INTEGRATION', 'FORMAL')), "
        "ADD CONSTRAINT ck_source_control_mr_binding_target CHECK ("
        "(kind='INTEGRATION' AND target_branch='dev') OR "
        "(kind='FORMAL' AND target_branch='main')), "
        "ADD CONSTRAINT ck_sc_mr_binding_supersession CHECK ("
        "superseded_at IS NULL OR superseded_at >= created_at)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sc_current_mr_binding_kind_work_item "
        "ON source_control.merge_request_binding (kind, work_item_id) "
        "WHERE superseded_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sc_current_mr_binding_kind_branch "
        "ON source_control.merge_request_binding (kind, branch_binding_id) "
        "WHERE superseded_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_sc_mr_binding_kind_work_item_history "
        "ON source_control.merge_request_binding (kind, work_item_id, created_at, id)"
    )
    op.execute(
        "ALTER TABLE source_control.delivery_request_inbox "
        "DROP CONSTRAINT fk_source_control_delivery_mr_binding, "
        "ADD COLUMN integration_binding_kind TEXT NOT NULL DEFAULT 'INTEGRATION', "
        "ADD CONSTRAINT ck_sc_delivery_inbox_binding_kind CHECK ("
        "integration_binding_kind='INTEGRATION'), "
        "ADD CONSTRAINT fk_source_control_delivery_mr_binding FOREIGN KEY ("
        "integration_merge_request_binding_id, work_item_id, requirement_id, "
        "integration_binding_kind) REFERENCES source_control.merge_request_binding ("
        "id, work_item_id, requirement_id, kind)"
    )

    op.execute(
        """
        CREATE TABLE source_control.formal_delivery_request_inbox (
            message_id UUID PRIMARY KEY,
            topic TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            requirement_id UUID NOT NULL,
            requirement_revision INTEGER NOT NULL,
            work_item_id UUID NOT NULL,
            work_item_revision INTEGER NOT NULL,
            repository_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            acceptance_decision_id UUID NOT NULL,
            formal_merge_request_binding_id UUID,
            formal_binding_kind TEXT NOT NULL DEFAULT 'FORMAL',
            formal_review_decision_id UUID,
            requested_head_sha TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_error_code TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            CONSTRAINT fk_sc_formal_inbox_repository FOREIGN KEY (repository_id)
                REFERENCES source_control.workspace_repository(id),
            CONSTRAINT fk_sc_formal_inbox_binding FOREIGN KEY (
                formal_merge_request_binding_id,
                work_item_id,
                requirement_id,
                formal_binding_kind
            ) REFERENCES source_control.merge_request_binding (
                id, work_item_id, requirement_id, kind
            ),
            CONSTRAINT ck_sc_formal_inbox_binding_kind CHECK (
                formal_binding_kind='FORMAL'
            ),
            CONSTRAINT ck_sc_formal_inbox_topic CHECK (
                (
                    topic='requirement.formal-merge-request.requested'
                    AND formal_review_decision_id IS NULL
                ) OR (
                    topic='requirement.formal-merge.requested'
                    AND formal_merge_request_binding_id IS NOT NULL
                    AND formal_review_decision_id IS NOT NULL
                )
            ),
            CONSTRAINT ck_sc_formal_inbox_refs CHECK (
                payload_hash ~ '^sha256:[0-9a-f]{64}$'
                AND requested_head_sha ~ '^[0-9a-f]{40}$'
                AND length(btrim(actor_id)) > 0
            ),
            CONSTRAINT ck_sc_formal_inbox_revisions CHECK (
                requirement_revision >= 1 AND work_item_revision >= 1
            ),
            CONSTRAINT ck_sc_formal_inbox_state CHECK (
                state IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')
            ),
            CONSTRAINT ck_sc_formal_inbox_attempts CHECK (attempts >= 0),
            CONSTRAINT ck_sc_formal_inbox_completion CHECK (
                (state='PROCESSED' AND processed_at IS NOT NULL)
                OR (state<>'PROCESSED' AND processed_at IS NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sc_formal_inbox_claim "
        "ON source_control.formal_delivery_request_inbox (available_at, message_id) "
        "WHERE state IN ('RECEIVED', 'PROCESSING', 'FAILED')"
    )

    op.execute(
        """
        CREATE TABLE source_control.formal_review_assignment (
            id UUID PRIMARY KEY,
            binding_id UUID NOT NULL,
            acceptance_decision_id UUID NOT NULL,
            requirement_id UUID NOT NULL,
            work_item_id UUID NOT NULL,
            binding_kind TEXT NOT NULL DEFAULT 'FORMAL',
            subject_head_sha TEXT NOT NULL,
            default_reviewer_id TEXT NOT NULL,
            current_reviewer_id TEXT NOT NULL,
            policy_code TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            policy_snapshot_hash TEXT NOT NULL,
            resolution_snapshot JSONB NOT NULL,
            revision INTEGER NOT NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            superseded_at TIMESTAMPTZ,
            CONSTRAINT fk_sc_formal_review_binding FOREIGN KEY (
                binding_id, work_item_id, requirement_id, binding_kind
            ) REFERENCES source_control.merge_request_binding (
                id, work_item_id, requirement_id, kind
            ),
            CONSTRAINT uq_sc_formal_review_owner UNIQUE (id, binding_id),
            CONSTRAINT uq_sc_formal_review_acceptance UNIQUE (
                binding_id, acceptance_decision_id
            ),
            CONSTRAINT uq_sc_formal_review_revision UNIQUE (binding_id, revision),
            CONSTRAINT ck_sc_formal_review_values CHECK (
                binding_kind='FORMAL'
                AND
                subject_head_sha ~ '^[0-9a-f]{40}$'
                AND length(btrim(default_reviewer_id)) > 0
                AND length(btrim(current_reviewer_id)) > 0
                AND length(btrim(policy_code)) > 0
                AND policy_version >= 1
                AND policy_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
                AND jsonb_typeof(resolution_snapshot)='object'
                AND revision >= 1
            ),
            CONSTRAINT ck_sc_formal_review_times CHECK (
                superseded_at IS NULL OR superseded_at >= assigned_at
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sc_current_formal_review "
        "ON source_control.formal_review_assignment (binding_id) "
        "WHERE superseded_at IS NULL"
    )
    op.execute(
        "GRANT SELECT, INSERT ON source_control.formal_delivery_request_inbox TO source_control_rw"
    )
    op.execute(
        "GRANT UPDATE (state, attempts, available_at, last_error_code, updated_at, "
        "processed_at) ON source_control.formal_delivery_request_inbox "
        "TO source_control_rw"
    )
    op.execute(
        "GRANT SELECT, INSERT ON source_control.formal_review_assignment TO source_control_rw"
    )
    op.execute(
        "GRANT UPDATE (superseded_at) ON source_control.formal_review_assignment "
        "TO source_control_rw"
    )
    op.execute(
        "GRANT UPDATE (superseded_at) ON source_control.merge_request_binding TO source_control_rw"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
            IF EXISTS (SELECT 1 FROM source_control.formal_delivery_request_inbox)
                OR EXISTS (SELECT 1 FROM source_control.formal_review_assignment)
                OR EXISTS (
                    SELECT 1 FROM source_control.merge_request_binding WHERE kind='FORMAL'
                )
                OR EXISTS (
                    SELECT 1 FROM source_control.source_control_effect
                    WHERE operation IN ('CREATE_FORMAL_MR', 'MERGE_FORMAL_MR')
                )
                OR EXISTS (
                    SELECT 1 FROM source_control.merge_request_binding
                    WHERE superseded_at IS NOT NULL
                )
                OR EXISTS (
                    SELECT work_item_id FROM source_control.source_control_effect
                    WHERE operation='CREATE_INTEGRATION_MR'
                    GROUP BY work_item_id HAVING count(*) > 1
                )
            THEN
                RAISE EXCEPTION 'formal delivery facts prevent Source Control downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "REVOKE ALL ON source_control.formal_delivery_request_inbox, "
        "source_control.formal_review_assignment FROM source_control_rw"
    )
    op.execute(
        "REVOKE UPDATE (superseded_at) ON source_control.merge_request_binding "
        "FROM source_control_rw"
    )
    op.execute("DROP TABLE source_control.formal_review_assignment")
    op.execute("DROP TABLE source_control.formal_delivery_request_inbox")
    op.execute(
        "ALTER TABLE source_control.delivery_request_inbox "
        "DROP CONSTRAINT fk_source_control_delivery_mr_binding, "
        "DROP CONSTRAINT ck_sc_delivery_inbox_binding_kind, "
        "DROP COLUMN integration_binding_kind, "
        "ADD CONSTRAINT fk_source_control_delivery_mr_binding "
        "FOREIGN KEY (integration_merge_request_binding_id) "
        "REFERENCES source_control.merge_request_binding(id)"
    )
    op.execute(
        "ALTER TABLE source_control.merge_request_binding "
        "DROP CONSTRAINT fk_source_control_mr_binding_effect, "
        "DROP COLUMN create_effect_operation, "
        "ADD CONSTRAINT fk_source_control_mr_binding_effect "
        "FOREIGN KEY (create_effect_id) "
        "REFERENCES source_control.source_control_effect(id)"
    )
    op.execute(
        "ALTER TABLE source_control.source_control_effect DROP CONSTRAINT uq_sc_effect_id_operation"
    )
    op.execute(
        "ALTER TABLE source_control.merge_request_binding "
        "DROP CONSTRAINT ck_source_control_mr_binding_target, "
        "DROP CONSTRAINT ck_source_control_mr_binding_kind, "
        "DROP CONSTRAINT ck_sc_mr_binding_supersession"
    )
    op.execute("DROP INDEX source_control.uq_sc_current_mr_binding_kind_branch")
    op.execute("DROP INDEX source_control.uq_sc_current_mr_binding_kind_work_item")
    op.execute("DROP INDEX source_control.ix_sc_mr_binding_kind_work_item_history")
    op.execute(
        "ALTER TABLE source_control.merge_request_binding "
        "DROP COLUMN superseded_at, "
        "ADD CONSTRAINT uq_source_control_mr_binding_work_item UNIQUE (work_item_id), "
        "ADD CONSTRAINT uq_source_control_mr_binding_branch UNIQUE (branch_binding_id), "
        "ADD CONSTRAINT ck_source_control_mr_binding_kind CHECK (kind='INTEGRATION'), "
        "ADD CONSTRAINT ck_source_control_mr_binding_target CHECK (target_branch='dev')"
    )
    op.execute(
        "ALTER TABLE source_control.source_control_effect "
        "DROP CONSTRAINT ck_source_control_effect_operation_shape"
    )
    op.execute(
        "UPDATE source_control.source_control_effect SET subject_key="
        "'work-item:' || work_item_id::text, "
        "effect_key='source-control:create-integration-mr:' || work_item_id::text "
        "WHERE operation='CREATE_INTEGRATION_MR'"
    )
    op.execute(
        """
        ALTER TABLE source_control.source_control_effect
        ADD CONSTRAINT ck_source_control_effect_operation_shape CHECK (
            (
                operation='CREATE_TASK_BRANCH'
                AND subject_key='work-item:' || work_item_id::text
                AND work_item_number IS NOT NULL AND work_item_number >= 1
                AND branch_name IS NOT NULL AND length(btrim(branch_name)) > 0
                AND base_commit_sha IS NOT NULL AND length(btrim(base_commit_sha)) > 0
                AND payload='{}'::jsonb
            ) OR (
                operation='CREATE_INTEGRATION_MR'
                AND subject_key='work-item:' || work_item_id::text
                AND work_item_number IS NULL AND branch_name IS NULL
                AND base_commit_sha IS NULL
                AND payload=jsonb_build_object(
                    'branchBindingId', payload -> 'branchBindingId',
                    'headSha', payload -> 'headSha'
                )
                AND jsonb_typeof(payload -> 'branchBindingId')='string'
                AND length(btrim(payload ->> 'branchBindingId')) > 0
                AND (payload ->> 'headSha') ~ '^[0-9a-f]{40}$'
            ) OR (
                operation='MERGE_INTEGRATION_MR'
                AND work_item_number IS NULL AND branch_name IS NULL
                AND base_commit_sha IS NULL
                AND payload=jsonb_build_object(
                    'bindingId', payload -> 'bindingId',
                    'requestedHeadSha', payload -> 'requestedHeadSha'
                )
                AND jsonb_typeof(payload -> 'bindingId')='string'
                AND length(btrim(payload ->> 'bindingId')) > 0
                AND (payload ->> 'requestedHeadSha') ~ '^[0-9a-f]{40}$'
                AND subject_key='mr:' || (payload ->> 'bindingId') || ':' ||
                    (payload ->> 'requestedHeadSha')
            )
        )
        """
    )
