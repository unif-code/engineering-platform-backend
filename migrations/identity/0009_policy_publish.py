"""identity-owned policy publish, rollback provenance, preview, and outbox."""

from alembic import op

revision = "0009_identity_policy_publish"
down_revision = "0008_identity_policy_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE identity.draft "
        "ADD COLUMN rollback_from_version BIGINT, "
        "ADD COLUMN preview_evidence JSONB, "
        "ADD COLUMN preview_content_hash TEXT, "
        "ADD COLUMN preview_schema_revision INTEGER, "
        "ADD COLUMN preview_base_version BIGINT, "
        "ADD COLUMN preview_dependency_versions JSONB"
    )
    op.execute(
        "ALTER TABLE identity.draft ADD CONSTRAINT ck_identity_draft_rollback_version "
        "CHECK (rollback_from_version IS NULL OR rollback_from_version > 0)"
    )
    op.execute(
        "ALTER TABLE identity.draft ADD CONSTRAINT ck_identity_draft_preview_binding CHECK ("
        "(preview_evidence IS NULL AND preview_content_hash IS NULL "
        "AND preview_schema_revision IS NULL AND preview_base_version IS NULL "
        "AND preview_dependency_versions IS NULL) OR "
        "(jsonb_typeof(preview_evidence) = 'object' "
        "AND preview_content_hash = content_hash "
        "AND preview_schema_revision = schema_revision "
        "AND preview_base_version = base_version "
        "AND jsonb_typeof(preview_dependency_versions) = 'object'))"
    )
    op.execute(
        "ALTER TABLE identity.version ADD COLUMN activated_at TIMESTAMPTZ "
        "NOT NULL DEFAULT transaction_timestamp()"
    )
    op.execute("UPDATE identity.version SET activated_at=published_at")
    op.execute(
        """
        CREATE TABLE identity.configuration_outbox (
            id UUID PRIMARY KEY,
            namespace TEXT NOT NULL,
            scope TEXT NOT NULL,
            event_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            payload JSONB NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            delivered_at TIMESTAMPTZ,
            CONSTRAINT ck_identity_configuration_outbox_namespace
                CHECK (namespace = 'identity'),
            CONSTRAINT ck_identity_configuration_outbox_scope
                CHECK (scope = 'PLATFORM'),
            CONSTRAINT ck_identity_configuration_outbox_event
                CHECK (event_type IN ('POLICY_PUBLISHED', 'DRAFT_ARCHIVED')),
            CONSTRAINT ck_identity_configuration_outbox_aggregate
                CHECK (length(btrim(aggregate_id)) > 0),
            CONSTRAINT ck_identity_configuration_outbox_payload
                CHECK (jsonb_typeof(payload) = 'object'),
            CONSTRAINT uq_identity_configuration_outbox_fact
                UNIQUE (event_type, aggregate_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_identity_configuration_outbox_pending "
        "ON identity.configuration_outbox(occurred_at, id) WHERE delivered_at IS NULL"
    )

    op.execute("GRANT INSERT ON identity.version TO configuration_rw")
    op.execute("GRANT UPDATE ON identity.active_pointer TO configuration_rw")
    op.execute("GRANT SELECT, INSERT ON identity.configuration_outbox TO configuration_rw")
    op.execute("GRANT SELECT ON identity.account TO configuration_rw")
    op.execute("GRANT UPDATE (totp_last_step, updated_at) ON identity.account TO configuration_rw")
    op.execute("GRANT SELECT ON identity.auth_challenge TO configuration_rw")
    op.execute(
        "GRANT INSERT (id, token_hash, purpose, account_id, actor_id, issued_at, "
        "expires_at, attempt_limit, attempt_count) "
        "ON identity.auth_challenge TO configuration_rw"
    )
    op.execute(
        "GRANT UPDATE (attempt_count, revoked_at, consumed_at) "
        "ON identity.auth_challenge TO configuration_rw"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE UPDATE (attempt_count, revoked_at, consumed_at) "
        "ON identity.auth_challenge FROM configuration_rw"
    )
    op.execute(
        "REVOKE INSERT (id, token_hash, purpose, account_id, actor_id, issued_at, "
        "expires_at, attempt_limit, attempt_count) "
        "ON identity.auth_challenge FROM configuration_rw"
    )
    op.execute("REVOKE SELECT ON identity.auth_challenge FROM configuration_rw")
    op.execute(
        "REVOKE UPDATE (totp_last_step, updated_at) ON identity.account FROM configuration_rw"
    )
    op.execute("REVOKE SELECT ON identity.account FROM configuration_rw")
    op.execute("REVOKE ALL ON identity.configuration_outbox FROM configuration_rw")
    op.execute("REVOKE UPDATE ON identity.active_pointer FROM configuration_rw")
    op.execute("REVOKE INSERT ON identity.version FROM configuration_rw")
    op.execute("DROP TABLE identity.configuration_outbox")
    op.execute("ALTER TABLE identity.version DROP COLUMN activated_at")
    op.execute("ALTER TABLE identity.draft DROP CONSTRAINT ck_identity_draft_preview_binding")
    op.execute("ALTER TABLE identity.draft DROP CONSTRAINT ck_identity_draft_rollback_version")
    op.execute(
        "ALTER TABLE identity.draft "
        "DROP COLUMN preview_dependency_versions, "
        "DROP COLUMN preview_base_version, "
        "DROP COLUMN preview_schema_revision, "
        "DROP COLUMN preview_content_hash, "
        "DROP COLUMN preview_evidence, "
        "DROP COLUMN rollback_from_version"
    )
