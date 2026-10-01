"""Grant Model Gateway runtime the audit-owned transactional append surface."""

from alembic import op

revision = "0009_audit_model_gateway_grant"
down_revision = "0008_audit_requirement_grant"
branch_labels = None
depends_on = "0001_model_gateway_catalog"

_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO model_gateway_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO model_gateway_rw")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM model_gateway_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM model_gateway_rw")
