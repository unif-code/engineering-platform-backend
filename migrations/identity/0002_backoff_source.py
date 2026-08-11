"""partition login backoff by employee number and request source."""

from alembic import op

revision = "0002_identity_backoff_source"
down_revision = "0001_identity_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE identity.login_backoff DROP CONSTRAINT login_backoff_pkey")
    op.execute(
        "ALTER TABLE identity.login_backoff ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy'"
    )
    op.execute(
        "ALTER TABLE identity.login_backoff "
        "ADD CONSTRAINT ck_identity_login_backoff_source CHECK (length(btrim(source)) > 0)"
    )
    op.execute(
        "ALTER TABLE identity.login_backoff "
        "ADD CONSTRAINT login_backoff_pkey PRIMARY KEY (employee_no, source)"
    )
    op.execute("ALTER TABLE identity.login_backoff ALTER COLUMN source DROP DEFAULT")


def downgrade() -> None:
    op.execute(
        """
        WITH merged AS (
            SELECT
                employee_no,
                LEAST(SUM(failure_count), 2147483647)::INTEGER AS failure_count,
                MAX(last_failure_at) AS last_failure_at,
                CASE
                    WHEN BOOL_OR(locked_until IS NOT NULL)
                    THEN GREATEST(MAX(locked_until), MAX(last_failure_at))
                    ELSE NULL
                END AS locked_until
            FROM identity.login_backoff
            GROUP BY employee_no
        ), cleared AS (
            DELETE FROM identity.login_backoff
        )
        INSERT INTO identity.login_backoff (
            employee_no, source, failure_count, last_failure_at, locked_until
        )
        SELECT employee_no, 'legacy', failure_count, last_failure_at, locked_until
        FROM merged
        """
    )
    op.execute("ALTER TABLE identity.login_backoff ALTER COLUMN source SET DEFAULT 'legacy'")
    op.execute("ALTER TABLE identity.login_backoff DROP CONSTRAINT login_backoff_pkey")
    op.execute(
        "ALTER TABLE identity.login_backoff DROP CONSTRAINT ck_identity_login_backoff_source"
    )
    op.execute("ALTER TABLE identity.login_backoff DROP COLUMN source")
    op.execute(
        "ALTER TABLE identity.login_backoff "
        "ADD CONSTRAINT login_backoff_pkey PRIMARY KEY (employee_no)"
    )
