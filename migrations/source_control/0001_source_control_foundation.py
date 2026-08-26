"""Source Control foundation: authorized repositories, effects, bindings, and inboxes.

The migration creates only a NOLOGIN privilege role. Infrastructure owns the
runtime login and credential lifecycle.
"""

from alembic import op

revision = "0001_source_control_foundation"
down_revision = None
branch_labels = ("source_control",)
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS source_control")
    op.execute("CREATE SEQUENCE source_control.work_item_number_seq START WITH 1")
    op.execute(
        """
        CREATE TABLE source_control.workspace_repository (
            id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL,
            provider TEXT NOT NULL,
            project_id TEXT NOT NULL,
            project_path TEXT NOT NULL,
            default_branch TEXT NOT NULL,
            connection_ref TEXT NOT NULL,
            credential_secret_ref TEXT NOT NULL,
            webhook_signing_secret_ref TEXT,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_source_control_provider CHECK (provider = 'GITLAB'),
            CONSTRAINT ck_source_control_default_branch CHECK (default_branch = 'main'),
            CONSTRAINT ck_source_control_repository_refs CHECK (
                length(btrim(project_id)) > 0
                AND length(btrim(project_path)) > 0
                AND length(btrim(connection_ref)) > 0
                AND length(btrim(credential_secret_ref)) > 0
                AND lower(credential_secret_ref) NOT LIKE '%glpat-%'
                AND (
                    webhook_signing_secret_ref IS NULL
                    OR (
                        length(btrim(webhook_signing_secret_ref)) > 0
                        AND lower(webhook_signing_secret_ref) NOT LIKE 'whsec\\_%' ESCAPE '\\'
                    )
                )
            ),
            CONSTRAINT ck_source_control_repository_status
                CHECK (status IN ('AUTHORIZED', 'REMOVED')),
            CONSTRAINT ck_source_control_repository_revision CHECK (revision >= 1),
            CONSTRAINT ck_source_control_repository_timestamps CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_source_control_provider_project "
        "ON source_control.workspace_repository (provider, project_id)"
    )
    op.execute(
        """
        CREATE TABLE source_control.binding_request_inbox (
            message_id UUID PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            requirement_id UUID NOT NULL,
            requirement_version INTEGER NOT NULL,
            work_item_id UUID NOT NULL,
            repository_id UUID NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_error_code TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            CONSTRAINT fk_source_control_inbox_repository
                FOREIGN KEY (repository_id)
                REFERENCES source_control.workspace_repository(id),
            CONSTRAINT ck_source_control_inbox_hash CHECK (length(btrim(payload_hash)) > 0),
            CONSTRAINT ck_source_control_inbox_requirement_version
                CHECK (requirement_version >= 1),
            CONSTRAINT ck_source_control_inbox_state
                CHECK (state IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')),
            CONSTRAINT ck_source_control_inbox_attempts CHECK (attempts >= 0),
            CONSTRAINT ck_source_control_inbox_completion CHECK (
                (state = 'PROCESSED' AND processed_at IS NOT NULL)
                OR (state <> 'PROCESSED' AND processed_at IS NULL)
            ),
            CONSTRAINT ck_source_control_inbox_timestamps CHECK (
                updated_at >= received_at
                AND (processed_at IS NULL OR processed_at BETWEEN received_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_source_control_binding_request_claim "
        "ON source_control.binding_request_inbox (available_at, message_id) "
        "WHERE state IN ('RECEIVED', 'FAILED')"
    )
    op.execute(
        """
        CREATE TABLE source_control.source_control_effect (
            id UUID PRIMARY KEY,
            effect_key TEXT NOT NULL,
            operation TEXT NOT NULL,
            work_item_id UUID NOT NULL,
            requirement_id UUID NOT NULL,
            repository_id UUID NOT NULL,
            work_item_number BIGINT NOT NULL,
            branch_name TEXT NOT NULL,
            base_commit_sha TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_reconcile_at TIMESTAMPTZ,
            state TEXT NOT NULL,
            last_error_code TEXT,
            requirement_callback_state TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT fk_source_control_effect_repository
                FOREIGN KEY (repository_id)
                REFERENCES source_control.workspace_repository(id),
            CONSTRAINT uq_source_control_effect_key UNIQUE (effect_key),
            CONSTRAINT uq_source_control_effect_work_item UNIQUE (work_item_id),
            CONSTRAINT uq_source_control_effect_number UNIQUE (work_item_number),
            CONSTRAINT ck_source_control_effect_operation
                CHECK (operation = 'CREATE_TASK_BRANCH'),
            CONSTRAINT ck_source_control_effect_refs CHECK (
                length(btrim(effect_key)) > 0
                AND length(btrim(branch_name)) > 0
                AND length(btrim(base_commit_sha)) > 0
                AND length(btrim(request_fingerprint)) > 0
            ),
            CONSTRAINT ck_source_control_effect_number CHECK (work_item_number >= 1),
            CONSTRAINT ck_source_control_effect_attempts CHECK (attempts >= 0),
            CONSTRAINT ck_source_control_effect_state CHECK (
                state IN (
                    'PLANNED', 'IN_FLIGHT', 'UNKNOWN', 'RECONCILIATION',
                    'SUCCEEDED', 'BLOCKED'
                )
            ),
            CONSTRAINT ck_source_control_effect_callback CHECK (
                requirement_callback_state IN ('PENDING', 'ACKED', 'FAILED')
            ),
            CONSTRAINT ck_source_control_effect_completion CHECK (
                (state IN ('SUCCEEDED', 'BLOCKED') AND completed_at IS NOT NULL)
                OR (state NOT IN ('SUCCEEDED', 'BLOCKED') AND completed_at IS NULL)
            ),
            CONSTRAINT ck_source_control_effect_timestamps CHECK (
                updated_at >= created_at
                AND (completed_at IS NULL OR completed_at BETWEEN created_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_source_control_effect_reconcile "
        "ON source_control.source_control_effect (next_reconcile_at, id) "
        "WHERE state = 'UNKNOWN'"
    )
    op.execute(
        """
        CREATE TABLE source_control.repository_branch_binding (
            id UUID PRIMARY KEY,
            work_item_id UUID NOT NULL,
            requirement_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            repository_id UUID NOT NULL,
            work_item_number BIGINT NOT NULL,
            base_commit_sha TEXT NOT NULL,
            branch_name TEXT NOT NULL,
            effect_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_source_control_binding_repository
                FOREIGN KEY (repository_id)
                REFERENCES source_control.workspace_repository(id),
            CONSTRAINT fk_source_control_binding_effect
                FOREIGN KEY (effect_id)
                REFERENCES source_control.source_control_effect(id),
            CONSTRAINT uq_source_control_binding_work_item UNIQUE (work_item_id),
            CONSTRAINT uq_source_control_binding_branch UNIQUE (repository_id, branch_name),
            CONSTRAINT uq_source_control_binding_effect UNIQUE (effect_id),
            CONSTRAINT ck_source_control_binding_number CHECK (work_item_number >= 1),
            CONSTRAINT ck_source_control_binding_refs CHECK (
                length(btrim(base_commit_sha)) > 0 AND length(btrim(branch_name)) > 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE source_control.webhook_inbox (
            id UUID PRIMARY KEY,
            repository_id UUID NOT NULL,
            webhook_id TEXT NOT NULL,
            webhook_timestamp TIMESTAMPTZ NOT NULL,
            payload_digest TEXT NOT NULL,
            provider_event_uuid TEXT,
            event_type TEXT NOT NULL,
            object_kind TEXT,
            project_id TEXT NOT NULL,
            ref TEXT,
            before_sha TEXT,
            after_sha TEXT,
            checkout_sha TEXT,
            state TEXT NOT NULL,
            last_error_code TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            CONSTRAINT fk_source_control_webhook_repository
                FOREIGN KEY (repository_id)
                REFERENCES source_control.workspace_repository(id),
            CONSTRAINT uq_source_control_webhook_message UNIQUE (repository_id, webhook_id),
            CONSTRAINT ck_source_control_webhook_refs CHECK (
                length(btrim(webhook_id)) > 0
                AND length(btrim(payload_digest)) > 0
                AND length(btrim(event_type)) > 0
                AND length(btrim(project_id)) > 0
            ),
            CONSTRAINT ck_source_control_webhook_state CHECK (
                state IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'IGNORED', 'FAILED')
            ),
            CONSTRAINT ck_source_control_webhook_completion CHECK (
                (state IN ('PROCESSED', 'IGNORED') AND processed_at IS NOT NULL)
                OR (state NOT IN ('PROCESSED', 'IGNORED') AND processed_at IS NULL)
            ),
            CONSTRAINT ck_source_control_webhook_timestamps CHECK (
                updated_at >= received_at
                AND (processed_at IS NULL OR processed_at BETWEEN received_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'source_control_rw') THEN
                CREATE ROLE source_control_rw NOLOGIN;
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA source_control TO source_control_rw")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON "
        "source_control.workspace_repository, "
        "source_control.binding_request_inbox, "
        "source_control.source_control_effect, "
        "source_control.webhook_inbox TO source_control_rw"
    )
    op.execute(
        "GRANT SELECT, INSERT ON source_control.repository_branch_binding TO source_control_rw"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE source_control.work_item_number_seq TO source_control_rw"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            business_rows BIGINT;
        BEGIN
            SELECT
                (SELECT count(*) FROM source_control.workspace_repository)
                + (SELECT count(*) FROM source_control.binding_request_inbox)
                + (SELECT count(*) FROM source_control.source_control_effect)
                + (SELECT count(*) FROM source_control.repository_branch_binding)
                + (SELECT count(*) FROM source_control.webhook_inbox)
            INTO business_rows;
            IF business_rows > 0 THEN
                RAISE EXCEPTION
                    'refusing Source Control downgrade: business rows still exist';
            END IF;
        END $$
        """
    )
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA source_control FROM source_control_rw")
    op.execute("REVOKE ALL ON SEQUENCE source_control.work_item_number_seq FROM source_control_rw")
    op.execute("REVOKE USAGE ON SCHEMA source_control FROM source_control_rw")
    op.execute("DROP TABLE source_control.webhook_inbox")
    op.execute("DROP TABLE source_control.repository_branch_binding")
    op.execute("DROP TABLE source_control.source_control_effect")
    op.execute("DROP TABLE source_control.binding_request_inbox")
    op.execute("DROP TABLE source_control.workspace_repository")
    op.execute("DROP SEQUENCE source_control.work_item_number_seq")
    op.execute("DROP SCHEMA source_control")
