"""add request correlation and least-privilege audit query support.

This successor preserves existing nullable rows while replacing the hardened
append function in place so every runtime append captures request context.
"""

from alembic import op

revision = "0007_audit_query_request_id"
down_revision = "0006_audit_configuration_grant"
branch_labels = None
depends_on = None

_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def _request_correlated_append_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION audit.append_event(
            p_id TEXT,
            p_occurred_at TIMESTAMPTZ,
            p_actor TEXT,
            p_actor_type TEXT,
            p_action TEXT,
            p_target_type TEXT,
            p_target_id TEXT,
            p_result TEXT,
            p_reason TEXT,
            p_correlation_id TEXT,
            p_schema_version INTEGER
        ) RETURNS VOID
        LANGUAGE SQL
        SECURITY DEFINER
        SET search_path = pg_catalog, audit
        AS $function$
            INSERT INTO audit.audit_event (
                id, occurred_at, actor, actor_type, action, target_type,
                target_id, result, reason, correlation_id, schema_version, request_id
            ) VALUES (
                p_id, p_occurred_at, p_actor, p_actor_type, p_action, p_target_type,
                p_target_id, p_result, p_reason, p_correlation_id, p_schema_version,
                NULLIF(current_setting('app.request_id', true), '')
            )
        $function$
    """


def _legacy_append_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION audit.append_event(
            p_id TEXT,
            p_occurred_at TIMESTAMPTZ,
            p_actor TEXT,
            p_actor_type TEXT,
            p_action TEXT,
            p_target_type TEXT,
            p_target_id TEXT,
            p_result TEXT,
            p_reason TEXT,
            p_correlation_id TEXT,
            p_schema_version INTEGER
        ) RETURNS VOID
        LANGUAGE SQL
        SECURITY DEFINER
        SET search_path = pg_catalog, audit
        AS $function$
            INSERT INTO audit.audit_event (
                id, occurred_at, actor, actor_type, action, target_type,
                target_id, result, reason, correlation_id, schema_version
            ) VALUES (
                p_id, p_occurred_at, p_actor, p_actor_type, p_action, p_target_type,
                p_target_id, p_result, p_reason, p_correlation_id, p_schema_version
            )
        $function$
    """


def upgrade() -> None:
    op.execute("ALTER TABLE audit.audit_event ADD COLUMN request_id TEXT")
    op.execute("CREATE INDEX ix_audit_event_occurred_id ON audit.audit_event (occurred_at, id)")
    op.execute(
        "CREATE INDEX ix_audit_event_request_occurred_id "
        "ON audit.audit_event (request_id, occurred_at, id) WHERE request_id IS NOT NULL"
    )
    op.execute(_request_correlated_append_function())
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")


def downgrade() -> None:
    op.execute(_legacy_append_function())
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute("DROP INDEX audit.ix_audit_event_request_occurred_id")
    op.execute("DROP INDEX audit.ix_audit_event_occurred_id")
    op.execute("ALTER TABLE audit.audit_event DROP COLUMN request_id")
