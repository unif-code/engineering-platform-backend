"""grant workspace runtime the audit-owned transactional append surface."""

from alembic import op

revision = "0004_audit_workspace_grant"
down_revision = "0003_audit_org_append_grant"
branch_labels = None
depends_on = "0001_workspace_base"

_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO workspace_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO workspace_rw")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM workspace_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM workspace_rw")
