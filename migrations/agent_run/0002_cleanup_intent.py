"""Persist the terminal intent needed to resume interrupted cleanup."""

from alembic import op

revision = "0002_agent_run_cleanup"
down_revision = "0001_agent_run_sandbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization ADD COLUMN cleanup_terminal_state TEXT"
    )
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization "
        "ADD CONSTRAINT ck_agent_run_materialization_cleanup_terminal "
        "CHECK (cleanup_terminal_state IS NULL OR cleanup_terminal_state IN "
        "('RELEASED', 'FINALIZED', 'CANCELED', 'TIMED_OUT'))"
    )
    op.execute(
        "GRANT UPDATE (cleanup_terminal_state) ON agent_run.sandbox_materialization TO agent_run_rw"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_run.sandbox_materialization "
        "DROP CONSTRAINT ck_agent_run_materialization_cleanup_terminal"
    )
    op.execute("ALTER TABLE agent_run.sandbox_materialization DROP COLUMN cleanup_terminal_state")
