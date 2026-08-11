"""workspace baseline: governance facts, members projection, and durable commands.

The migration creates only a NOLOGIN privilege role. Infrastructure owns the
runtime login and credential lifecycle.
"""

from alembic import op

revision = "0001_workspace_base"
down_revision = None
branch_labels = ("workspace",)
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS workspace")
    op.execute(
        """
        CREATE TABLE workspace.workspace (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            archived_at TIMESTAMPTZ,
            version INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT ck_workspace_name CHECK (length(btrim(name)) > 0),
            CONSTRAINT ck_workspace_owner_id CHECK (length(btrim(owner_id)) > 0),
            CONSTRAINT ck_workspace_version CHECK (version >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_workspace_owner_active "
        "ON workspace.workspace (owner_id, id) WHERE archived_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE workspace.leader (
            workspace_id UUID NOT NULL,
            account_id TEXT NOT NULL,
            invited_by TEXT NOT NULL,
            PRIMARY KEY (workspace_id, account_id),
            CONSTRAINT fk_workspace_leader_workspace
                FOREIGN KEY (workspace_id) REFERENCES workspace.workspace(id),
            CONSTRAINT ck_workspace_leader_account_id
                CHECK (length(btrim(account_id)) > 0),
            CONSTRAINT ck_workspace_leader_invited_by
                CHECK (length(btrim(invited_by)) > 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_workspace_leader_account ON workspace.leader (account_id, workspace_id)"
    )
    op.execute(
        """
        CREATE TABLE workspace.members_projection (
            workspace_id UUID NOT NULL,
            account_id TEXT NOT NULL,
            source TEXT NOT NULL,
            computed_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (workspace_id, account_id),
            CONSTRAINT fk_workspace_members_workspace
                FOREIGN KEY (workspace_id) REFERENCES workspace.workspace(id),
            CONSTRAINT ck_workspace_members_account_id
                CHECK (length(btrim(account_id)) > 0),
            CONSTRAINT ck_workspace_members_source
                CHECK (source IN ('OWNER', 'LEADER', 'DIRECT_REPORT'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_workspace_members_account "
        "ON workspace.members_projection (account_id, workspace_id)"
    )
    op.execute(
        """
        CREATE TABLE workspace.idempotency_record (
            id UUID PRIMARY KEY,
            actor TEXT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            http_status INTEGER,
            result_metadata JSONB,
            sealed_response BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT uq_workspace_idempotency_scope
                UNIQUE (actor, operation, idempotency_key),
            CONSTRAINT ck_workspace_idempotency_actor CHECK (length(actor) > 0),
            CONSTRAINT ck_workspace_idempotency_operation CHECK (length(operation) > 0),
            CONSTRAINT ck_workspace_idempotency_key CHECK (length(idempotency_key) > 0),
            CONSTRAINT ck_workspace_idempotency_fingerprint
                CHECK (length(request_fingerprint) > 0),
            CONSTRAINT ck_workspace_idempotency_state
                CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
            CONSTRAINT ck_workspace_idempotency_result
                CHECK (
                    (
                        state = 'IN_PROGRESS'
                        AND http_status IS NULL
                        AND result_metadata IS NULL
                        AND sealed_response IS NULL
                        AND completed_at IS NULL
                    )
                    OR
                    (
                        state = 'COMPLETED'
                        AND http_status BETWEEN 100 AND 599
                        AND result_metadata IS NOT NULL
                        AND sealed_response IS NOT NULL
                        AND completed_at IS NOT NULL
                    )
                ),
            CONSTRAINT ck_workspace_idempotency_timestamps
                CHECK (
                    updated_at >= created_at
                    AND (
                        completed_at IS NULL
                        OR (completed_at >= created_at AND completed_at <= updated_at)
                    )
                )
        )
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'workspace_rw') THEN
                CREATE ROLE workspace_rw NOLOGIN;
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA workspace TO workspace_rw")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON workspace.workspace, "
        "workspace.idempotency_record TO workspace_rw"
    )
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON workspace.leader, "
        "workspace.members_projection TO workspace_rw"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA workspace FROM workspace_rw")
    op.execute("REVOKE USAGE ON SCHEMA workspace FROM workspace_rw")
    op.execute("DROP TABLE workspace.idempotency_record")
    op.execute("DROP TABLE workspace.members_projection")
    op.execute("DROP TABLE workspace.leader")
    op.execute("DROP TABLE workspace.workspace")
    op.execute("DROP SCHEMA workspace")
