"""Grant Requirement runtime the audit-owned transactional append surface."""

from alembic import op

revision = "0008_audit_requirement_grant"
down_revision = "0007_audit_query_request_id"
branch_labels = None
depends_on = "0001_requirement_base"

_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO requirement_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO requirement_rw")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM requirement_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM requirement_rw")
