"""Add durable Source Control integration delivery facts."""

from alembic import op

revision = "0005_sc_int_delivery"
down_revision = "0004_sc_secret_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE source_control.source_control_effect "
        "ADD COLUMN subject_key TEXT, "
        "ADD COLUMN payload JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "UPDATE source_control.source_control_effect "
        "SET subject_key = 'work-item:' || work_item_id::text "
        "WHERE subject_key IS NULL"
    )
    op.execute(
        "ALTER TABLE source_control.source_control_effect "
        "ALTER COLUMN subject_key SET NOT NULL, "
        "ALTER COLUMN work_item_number DROP NOT NULL, "
        "ALTER COLUMN branch_name DROP NOT NULL, "
        "ALTER COLUMN base_commit_sha DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE source_control.source_control_effect "
        "DROP CONSTRAINT uq_source_control_effect_work_item, "
        "DROP CONSTRAINT uq_source_control_effect_number, "
        "DROP CONSTRAINT ck_source_control_effect_operation, "
        "DROP CONSTRAINT ck_source_control_effect_refs, "
        "DROP CONSTRAINT ck_source_control_effect_number"
    )
    op.execute(
        "ALTER TABLE source_control.source_control_effect "
        "ADD CONSTRAINT uq_source_control_effect_operation_subject "
        "UNIQUE (operation, subject_key)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_source_control_effect_branch_number "
        "ON source_control.source_control_effect (work_item_number) "
        "WHERE operation = 'CREATE_TASK_BRANCH'"
    )
    op.execute(
        """
        ALTER TABLE source_control.source_control_effect
        ADD CONSTRAINT ck_source_control_effect_refs CHECK (
            length(btrim(effect_key)) > 0
            AND length(btrim(subject_key)) > 0
            AND length(btrim(request_fingerprint)) > 0
            AND jsonb_typeof(payload) = 'object'
        ),
        ADD CONSTRAINT ck_source_control_effect_operation_shape CHECK (
            (
                operation = 'CREATE_TASK_BRANCH'
                AND subject_key = 'work-item:' || work_item_id::text
                AND work_item_number IS NOT NULL
                AND work_item_number >= 1
                AND branch_name IS NOT NULL
                AND length(btrim(branch_name)) > 0
                AND base_commit_sha IS NOT NULL
                AND length(btrim(base_commit_sha)) > 0
                AND payload = '{}'::jsonb
            )
            OR (
                operation = 'CREATE_INTEGRATION_MR'
                AND subject_key = 'work-item:' || work_item_id::text
                AND work_item_number IS NULL
                AND branch_name IS NULL
                AND base_commit_sha IS NULL
            )
            OR (
                operation = 'MERGE_INTEGRATION_MR'
                AND subject_key ~ '^mr:[^:]+:[0-9a-f]{40}$'
                AND work_item_number IS NULL
                AND branch_name IS NULL
                AND base_commit_sha IS NULL
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE source_control.merge_request_binding (
            id UUID PRIMARY KEY,
            kind TEXT NOT NULL,
            work_item_id UUID NOT NULL,
            requirement_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            repository_id TEXT NOT NULL,
            branch_binding_id UUID NOT NULL,
            external_project_id TEXT NOT NULL,
            merge_request_iid BIGINT NOT NULL,
            source_branch TEXT NOT NULL,
            target_branch TEXT NOT NULL,
            create_effect_id UUID NOT NULL,
            head_sha TEXT NOT NULL,
            creation_origin TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_source_control_mr_binding_repository
                FOREIGN KEY (repository_id)
                REFERENCES source_control.workspace_repository(id),
            CONSTRAINT fk_source_control_mr_binding_branch
                FOREIGN KEY (branch_binding_id)
                REFERENCES source_control.repository_branch_binding(id),
            CONSTRAINT fk_source_control_mr_binding_effect
                FOREIGN KEY (create_effect_id)
                REFERENCES source_control.source_control_effect(id),
            CONSTRAINT uq_source_control_mr_binding_work_item UNIQUE (work_item_id),
            CONSTRAINT uq_source_control_mr_binding_branch UNIQUE (branch_binding_id),
            CONSTRAINT uq_source_control_mr_binding_effect UNIQUE (create_effect_id),
            CONSTRAINT uq_source_control_mr_binding_external
                UNIQUE (repository_id, merge_request_iid),
            CONSTRAINT ck_source_control_mr_binding_kind CHECK (kind = 'INTEGRATION'),
            CONSTRAINT ck_source_control_mr_binding_iid CHECK (merge_request_iid >= 1),
            CONSTRAINT ck_source_control_mr_binding_target CHECK (target_branch = 'dev'),
            CONSTRAINT ck_source_control_mr_binding_origin CHECK (
                creation_origin IN ('PLATFORM_CREATED', 'EXTERNAL_ADOPTED')
            ),
            CONSTRAINT ck_source_control_mr_binding_refs CHECK (
                length(btrim(external_project_id)) > 0
                AND length(btrim(source_branch)) > 0
                AND length(btrim(head_sha)) > 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE source_control.merge_request_observation (
            id UUID PRIMARY KEY,
            binding_id UUID NOT NULL,
            head_sha TEXT NOT NULL,
            state TEXT NOT NULL,
            merge_commit_sha TEXT,
            external_merge_user_id TEXT,
            merged_at TIMESTAMPTZ,
            observation_digest TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT fk_source_control_mr_observation_binding
                FOREIGN KEY (binding_id)
                REFERENCES source_control.merge_request_binding(id),
            CONSTRAINT uq_source_control_mr_observation_digest
                UNIQUE (binding_id, observation_digest),
            CONSTRAINT ck_source_control_mr_observation_state CHECK (
                state IN ('OPEN', 'MERGED', 'CLOSED', 'LOCKED')
            ),
            CONSTRAINT ck_source_control_mr_observation_refs CHECK (
                length(btrim(head_sha)) > 0
                AND length(btrim(observation_digest)) > 0
            ),
            CONSTRAINT ck_source_control_mr_observation_merge CHECK (
                (
                    state = 'MERGED'
                    AND merge_commit_sha IS NOT NULL
                    AND length(btrim(merge_commit_sha)) > 0
                    AND merged_at IS NOT NULL
                )
                OR (
                    state <> 'MERGED'
                    AND merge_commit_sha IS NULL
                    AND external_merge_user_id IS NULL
                    AND merged_at IS NULL
                )
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_source_control_mr_observation_latest "
        "ON source_control.merge_request_observation (binding_id, observed_at DESC, id DESC)"
    )
    op.execute(
        """
        CREATE TABLE source_control.delivery_request_inbox (
            message_id UUID PRIMARY KEY,
            topic TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            requirement_id UUID NOT NULL,
            requirement_revision INTEGER NOT NULL,
            work_item_id UUID NOT NULL,
            work_item_revision INTEGER NOT NULL,
            repository_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            integration_merge_request_binding_id UUID,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_error_code TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            CONSTRAINT fk_source_control_delivery_repository
                FOREIGN KEY (repository_id)
                REFERENCES source_control.workspace_repository(id),
            CONSTRAINT fk_source_control_delivery_mr_binding
                FOREIGN KEY (integration_merge_request_binding_id)
                REFERENCES source_control.merge_request_binding(id),
            CONSTRAINT ck_source_control_delivery_refs CHECK (
                length(btrim(payload_hash)) > 0
                AND length(btrim(actor_id)) > 0
            ),
            CONSTRAINT ck_source_control_delivery_revisions CHECK (
                requirement_revision >= 1 AND work_item_revision >= 1
            ),
            CONSTRAINT ck_source_control_delivery_topic_shape CHECK (
                (
                    topic = 'requirement.integration-merge-request.requested'
                    AND integration_merge_request_binding_id IS NULL
                )
                OR (
                    topic = 'requirement.integration-merge.requested'
                    AND integration_merge_request_binding_id IS NOT NULL
                )
            ),
            CONSTRAINT ck_source_control_delivery_state CHECK (
                state IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')
            ),
            CONSTRAINT ck_source_control_delivery_attempts CHECK (attempts >= 0),
            CONSTRAINT ck_source_control_delivery_completion CHECK (
                (state = 'PROCESSED' AND processed_at IS NOT NULL)
                OR (state <> 'PROCESSED' AND processed_at IS NULL)
            ),
            CONSTRAINT ck_source_control_delivery_timestamps CHECK (
                updated_at >= received_at
                AND (processed_at IS NULL OR processed_at BETWEEN received_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_source_control_delivery_claim "
        "ON source_control.delivery_request_inbox (available_at, message_id) "
        "WHERE state IN ('RECEIVED', 'PROCESSING', 'FAILED')"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON source_control.delivery_request_inbox TO source_control_rw"
    )
    op.execute(
        "GRANT SELECT, INSERT ON "
        "source_control.merge_request_binding, "
        "source_control.merge_request_observation TO source_control_rw"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
            IF EXISTS (SELECT 1 FROM source_control.delivery_request_inbox)
                OR EXISTS (SELECT 1 FROM source_control.merge_request_binding)
                OR EXISTS (SELECT 1 FROM source_control.merge_request_observation)
                OR EXISTS (
                    SELECT 1 FROM source_control.source_control_effect
                    WHERE operation <> 'CREATE_TASK_BRANCH'
                )
            THEN
                RAISE EXCEPTION
                    'integration delivery facts prevent Source Control downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "REVOKE ALL ON source_control.delivery_request_inbox, "
        "source_control.merge_request_binding, "
        "source_control.merge_request_observation FROM source_control_rw"
    )
    op.execute("DROP TABLE source_control.delivery_request_inbox")
    op.execute("DROP TABLE source_control.merge_request_observation")
    op.execute("DROP TABLE source_control.merge_request_binding")
    op.execute("DROP INDEX source_control.uq_source_control_effect_branch_number")
    op.execute(
        "ALTER TABLE source_control.source_control_effect "
        "DROP CONSTRAINT uq_source_control_effect_operation_subject, "
        "DROP CONSTRAINT ck_source_control_effect_operation_shape, "
        "DROP CONSTRAINT ck_source_control_effect_refs"
    )
    op.execute(
        "ALTER TABLE source_control.source_control_effect "
        "ALTER COLUMN work_item_number SET NOT NULL, "
        "ALTER COLUMN branch_name SET NOT NULL, "
        "ALTER COLUMN base_commit_sha SET NOT NULL, "
        "DROP COLUMN payload, "
        "DROP COLUMN subject_key"
    )
    op.execute(
        """
        ALTER TABLE source_control.source_control_effect
        ADD CONSTRAINT uq_source_control_effect_work_item UNIQUE (work_item_id),
        ADD CONSTRAINT uq_source_control_effect_number UNIQUE (work_item_number),
        ADD CONSTRAINT ck_source_control_effect_operation
            CHECK (operation = 'CREATE_TASK_BRANCH'),
        ADD CONSTRAINT ck_source_control_effect_refs CHECK (
            length(btrim(effect_key)) > 0
            AND length(btrim(branch_name)) > 0
            AND length(btrim(base_commit_sha)) > 0
            AND length(btrim(request_fingerprint)) > 0
        ),
        ADD CONSTRAINT ck_source_control_effect_number CHECK (work_item_number >= 1)
        """
    )
