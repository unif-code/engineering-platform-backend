"""audit-owned transactional append surface for identity runtime."""

from alembic import op

revision = "0002_audit_transactional_append"
down_revision = "0001_audit_event"
branch_labels = None
depends_on = "0001_identity_base"

_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION audit.append_event(
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
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA audit TO identity_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO identity_rw")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM identity_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM identity_rw")
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
