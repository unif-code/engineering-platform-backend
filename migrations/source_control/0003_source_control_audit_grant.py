"""Grant Source Control runtime the audit-owned transactional append surface."""

from alembic import op

revision = "0003_sc_audit_grant"
down_revision = "0002_sc_repository_text"
branch_labels = None
depends_on = "0007_audit_query_request_id"

_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO source_control_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO source_control_rw")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM source_control_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM source_control_rw")
