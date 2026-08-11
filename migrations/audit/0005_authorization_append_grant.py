"""grant authorization runtime the audit-owned transactional append surface."""

from alembic import op

revision = "0005_audit_authz_grant"
down_revision = "0004_audit_workspace_grant"
branch_labels = None
depends_on = "0001_authorization_base"

_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO authorization_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO authorization_rw")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM authorization_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM authorization_rw")
