"""grant organization runtime the audit-owned transactional append surface."""

from alembic import op

revision = "0003_audit_org_append_grant"
down_revision = "0002_audit_transactional_append"
branch_labels = None
depends_on = "0001_organization_base"

_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO organization_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO organization_rw")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM organization_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM organization_rw")
