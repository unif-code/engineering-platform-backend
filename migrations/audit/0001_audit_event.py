"""audit baseline: schema, append-only audit_event, audit_rw role.

角色口令仅用于本地/CI（生产角色由基础设施子项目管理）。
"""

from alembic import op

revision = "0001_audit_event"
down_revision = None
branch_labels = ("audit",)
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")
    op.execute(
        """
        CREATE TABLE audit.audit_event (
            id TEXT PRIMARY KEY,
            occurred_at TIMESTAMPTZ NOT NULL,
            actor TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            result TEXT NOT NULL,
            reason TEXT,
            correlation_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        )
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'audit_rw') THEN
                CREATE ROLE audit_rw LOGIN PASSWORD 'localdev';
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA audit TO audit_rw")
    op.execute("GRANT SELECT, INSERT ON audit.audit_event TO audit_rw")


def downgrade() -> None:
    op.execute("REVOKE ALL ON audit.audit_event FROM audit_rw")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM audit_rw")
    op.execute("DROP TABLE audit.audit_event")
    op.execute("DROP SCHEMA audit")
