"""grant configuration runtime the audit-owned transactional append surface."""

from alembic import op

revision = "0006_audit_configuration_grant"
down_revision = "0005_audit_authz_grant"
branch_labels = None
depends_on = "0005_identity_configuration"

_SIGNATURE = "audit.append_event(text,timestamptz,text,text,text,text,text,text,text,text,integer)"


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO configuration_rw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO configuration_rw")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM configuration_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM configuration_rw")
