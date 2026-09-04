"""Add immutable external validation and Integration Baseline Evidence facts."""

from alembic import op

revision = "0007_sc_evidence"
down_revision = "0006_sc_mr_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE source_control.merge_request_binding "
        "ADD CONSTRAINT uq_sc_mr_binding_owner "
        "UNIQUE (id, work_item_id, requirement_id, workspace_id, kind), "
        "ADD CONSTRAINT uq_sc_mr_binding_subject "
        "UNIQUE (id, work_item_id, requirement_id, kind)"
    )
    op.execute(
        """
        CREATE TABLE source_control.external_validation_reference (
            id UUID PRIMARY KEY,
            work_item_id UUID NOT NULL,
            requirement_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            integration_merge_request_binding_id UUID NOT NULL,
            integration_binding_kind TEXT NOT NULL DEFAULT 'INTEGRATION',
            target_commit_sha TEXT NOT NULL,
            integration_merge_commit_sha TEXT NOT NULL,
            reference TEXT NOT NULL,
            notes TEXT NOT NULL,
            artifact_references JSONB NOT NULL,
            reference_hash TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            submitted_by TEXT NOT NULL,
            submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_sc_validation_binding FOREIGN KEY (
                integration_merge_request_binding_id,
                work_item_id,
                requirement_id,
                workspace_id,
                integration_binding_kind
            ) REFERENCES source_control.merge_request_binding (
                id, work_item_id, requirement_id, workspace_id, kind
            ),
            CONSTRAINT uq_sc_validation_owner UNIQUE (id, requirement_id, work_item_id),
            CONSTRAINT uq_sc_validation_hash UNIQUE (work_item_id, reference_hash),
            CONSTRAINT ck_sc_validation_commits CHECK (
                target_commit_sha ~ '^[0-9a-f]{40}$'
                AND integration_merge_commit_sha ~ '^[0-9a-f]{40}$'
            ),
            CONSTRAINT ck_sc_validation_binding_kind CHECK (
                integration_binding_kind='INTEGRATION'
            ),
            CONSTRAINT ck_sc_validation_reference CHECK (
                length(btrim(reference)) > 0
                AND position('?' in reference) = 0
                AND position('#' in reference) = 0
                AND (
                    reference ~ '^https?://[^/@[:space:]]+(/[^?#]*)?$'
                    OR reference ~ '^urn:[^[:space:]]+$'
                )
            ),
            CONSTRAINT ck_sc_validation_notes_actor CHECK (
                length(btrim(notes)) > 0 AND length(btrim(submitted_by)) > 0
            ),
            CONSTRAINT ck_sc_validation_artifacts CHECK (
                jsonb_typeof(artifact_references) = 'array'
                AND jsonb_array_length(artifact_references) >= 1
            ),
            CONSTRAINT ck_sc_validation_hash CHECK (
                reference_hash ~ '^sha256:[0-9a-f]{64}$'
                AND request_fingerprint ~ '^sha256:[0-9a-f]{64}$'
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sc_validation_current "
        "ON source_control.external_validation_reference "
        "(work_item_id, submitted_at DESC, id DESC)"
    )
    op.execute(
        """
        CREATE TABLE source_control.external_validation_receipt (
            message_id UUID PRIMARY KEY,
            request_fingerprint TEXT NOT NULL,
            outcome TEXT NOT NULL,
            canonical_external_validation_id UUID,
            requirement_id UUID NOT NULL,
            work_item_id UUID NOT NULL,
            rejection_reason_code TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_sc_validation_receipt_fact FOREIGN KEY (
                canonical_external_validation_id,
                requirement_id,
                work_item_id
            ) REFERENCES source_control.external_validation_reference (
                id, requirement_id, work_item_id
            ),
            CONSTRAINT ck_sc_validation_receipt_fingerprint CHECK (
                request_fingerprint ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_sc_validation_receipt_outcome CHECK (
                (
                    outcome='ACCEPTED'
                    AND canonical_external_validation_id IS NOT NULL
                    AND rejection_reason_code IS NULL
                ) OR (
                    outcome='REJECTED'
                    AND canonical_external_validation_id IS NULL
                    AND rejection_reason_code='EVIDENCE_STALE'
                )
            )
        )
        """
    )

    op.execute(
        """
        CREATE TABLE source_control.evidence_request_inbox (
            message_id UUID PRIMARY KEY,
            topic TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            delivery_snapshot_id UUID NOT NULL,
            delivery_snapshot_hash TEXT NOT NULL,
            requirement_id UUID NOT NULL,
            requirement_version INTEGER NOT NULL,
            required_work_item_set_version INTEGER NOT NULL,
            required_work_item_set_hash TEXT NOT NULL,
            work_item_ids JSONB NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_error_code TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            CONSTRAINT ck_sc_evidence_inbox_topic CHECK (
                topic = 'requirement.integration-baseline.requested'
            ),
            CONSTRAINT ck_sc_evidence_inbox_hashes CHECK (
                payload_hash ~ '^sha256:[0-9a-f]{64}$'
                AND delivery_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
                AND required_work_item_set_hash ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_sc_evidence_inbox_versions CHECK (
                requirement_version >= 1 AND required_work_item_set_version >= 1
            ),
            CONSTRAINT ck_sc_evidence_inbox_items CHECK (
                jsonb_typeof(work_item_ids) = 'array'
                AND jsonb_array_length(work_item_ids) >= 1
            ),
            CONSTRAINT ck_sc_evidence_inbox_state CHECK (
                state IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')
            ),
            CONSTRAINT ck_sc_evidence_inbox_attempts CHECK (attempts >= 0),
            CONSTRAINT ck_sc_evidence_inbox_completion CHECK (
                (state = 'PROCESSED' AND processed_at IS NOT NULL)
                OR (state <> 'PROCESSED' AND processed_at IS NULL)
            ),
            CONSTRAINT ck_sc_evidence_inbox_times CHECK (
                updated_at >= received_at
                AND (processed_at IS NULL OR processed_at BETWEEN received_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sc_evidence_inbox_claim "
        "ON source_control.evidence_request_inbox (available_at, message_id) "
        "WHERE state IN ('RECEIVED', 'PROCESSING', 'FAILED')"
    )

    op.execute(
        """
        CREATE TABLE source_control.integration_baseline_evidence (
            id UUID PRIMARY KEY,
            delivery_snapshot_id UUID NOT NULL,
            delivery_snapshot_hash TEXT NOT NULL,
            requirement_id UUID NOT NULL,
            requirement_version INTEGER NOT NULL,
            required_work_item_set_version INTEGER NOT NULL,
            required_work_item_set_hash TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            generated_by TEXT NOT NULL,
            generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_sc_evidence_owner UNIQUE (id, requirement_id),
            CONSTRAINT uq_sc_evidence_snapshot UNIQUE (
                delivery_snapshot_id, delivery_snapshot_hash
            ),
            CONSTRAINT uq_sc_evidence_hash UNIQUE (requirement_id, evidence_hash),
            CONSTRAINT ck_sc_evidence_versions CHECK (
                requirement_version >= 1 AND required_work_item_set_version >= 1
            ),
            CONSTRAINT ck_sc_evidence_hashes CHECK (
                delivery_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
                AND required_work_item_set_hash ~ '^sha256:[0-9a-f]{64}$'
                AND evidence_hash ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_sc_evidence_actor CHECK (length(btrim(generated_by)) > 0)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE source_control.integration_baseline_evidence_item (
            evidence_id UUID NOT NULL,
            requirement_id UUID NOT NULL,
            work_item_id UUID NOT NULL,
            repository_id TEXT NOT NULL,
            task_branch TEXT NOT NULL,
            task_commit_sha TEXT NOT NULL,
            integration_merge_request_binding_id UUID NOT NULL,
            integration_binding_kind TEXT NOT NULL DEFAULT 'INTEGRATION',
            integration_merge_request_iid BIGINT NOT NULL,
            integration_merge_commit_sha TEXT NOT NULL,
            executor_type TEXT NOT NULL,
            executor_id TEXT NOT NULL,
            artifact_references JSONB NOT NULL,
            external_validation_reference_id UUID NOT NULL,
            item_hash TEXT NOT NULL,
            CONSTRAINT pk_sc_evidence_item PRIMARY KEY (evidence_id, work_item_id),
            CONSTRAINT fk_sc_evidence_item_evidence FOREIGN KEY (
                evidence_id, requirement_id
            ) REFERENCES source_control.integration_baseline_evidence (id, requirement_id),
            CONSTRAINT fk_sc_evidence_item_repository
                FOREIGN KEY (repository_id)
                REFERENCES source_control.workspace_repository(id),
            CONSTRAINT fk_sc_evidence_item_binding FOREIGN KEY (
                integration_merge_request_binding_id,
                work_item_id,
                requirement_id,
                integration_binding_kind
            ) REFERENCES source_control.merge_request_binding (
                id, work_item_id, requirement_id, kind
            ),
            CONSTRAINT fk_sc_evidence_item_validation FOREIGN KEY (
                external_validation_reference_id, requirement_id, work_item_id
            ) REFERENCES source_control.external_validation_reference (
                id, requirement_id, work_item_id
            ),
            CONSTRAINT ck_sc_evidence_item_refs CHECK (
                length(btrim(task_branch)) > 0
                AND length(btrim(executor_id)) > 0
                AND task_commit_sha ~ '^[0-9a-f]{40}$'
                AND integration_merge_commit_sha ~ '^[0-9a-f]{40}$'
                AND integration_merge_request_iid >= 1
            ),
            CONSTRAINT ck_sc_evidence_item_executor CHECK (executor_type = 'HUMAN'),
            CONSTRAINT ck_sc_evidence_item_binding_kind CHECK (
                integration_binding_kind='INTEGRATION'
            ),
            CONSTRAINT ck_sc_evidence_item_artifacts CHECK (
                jsonb_typeof(artifact_references) = 'array'
                AND jsonb_array_length(artifact_references) >= 1
            ),
            CONSTRAINT ck_sc_evidence_item_hash CHECK (
                item_hash ~ '^sha256:[0-9a-f]{64}$'
            )
        )
        """
    )

    op.execute(
        "GRANT SELECT, INSERT ON "
        "source_control.external_validation_reference, "
        "source_control.external_validation_receipt, "
        "source_control.integration_baseline_evidence, "
        "source_control.integration_baseline_evidence_item TO source_control_rw"
    )
    op.execute("GRANT SELECT, INSERT ON source_control.evidence_request_inbox TO source_control_rw")
    op.execute(
        "GRANT UPDATE (state, attempts, available_at, last_error_code, updated_at, "
        "processed_at) ON source_control.evidence_request_inbox TO source_control_rw"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
            IF EXISTS (SELECT 1 FROM source_control.external_validation_receipt)
                OR EXISTS (SELECT 1 FROM source_control.external_validation_reference)
                OR EXISTS (SELECT 1 FROM source_control.evidence_request_inbox)
                OR EXISTS (SELECT 1 FROM source_control.integration_baseline_evidence)
                OR EXISTS (SELECT 1 FROM source_control.integration_baseline_evidence_item)
            THEN
                RAISE EXCEPTION 'V0.6 Evidence facts prevent Source Control downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "REVOKE ALL ON "
        "source_control.external_validation_receipt, "
        "source_control.external_validation_reference, "
        "source_control.evidence_request_inbox, "
        "source_control.integration_baseline_evidence, "
        "source_control.integration_baseline_evidence_item FROM source_control_rw"
    )
    op.execute("DROP TABLE source_control.integration_baseline_evidence_item")
    op.execute("DROP TABLE source_control.integration_baseline_evidence")
    op.execute("DROP TABLE source_control.evidence_request_inbox")
    op.execute("DROP TABLE source_control.external_validation_receipt")
    op.execute("DROP TABLE source_control.external_validation_reference")
    op.execute(
        "ALTER TABLE source_control.merge_request_binding "
        "DROP CONSTRAINT uq_sc_mr_binding_subject, "
        "DROP CONSTRAINT uq_sc_mr_binding_owner"
    )
