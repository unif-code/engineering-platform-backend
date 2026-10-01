"""Grant the independent Model Gateway worker only the audit append surface."""

from alembic import op

revision = "0010_audit_model_worker_grant"
down_revision = "0009_audit_model_gateway_grant"
branch_labels = None
depends_on = "0002_model_connection_checks"
_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO model_gateway_worker_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO model_gateway_worker_rw")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM model_gateway_worker_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM model_gateway_worker_rw")
