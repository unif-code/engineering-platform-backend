"""organization baseline: fixed-level edges, durable commands, and runtime role.

角色口令仅用于本地/CI（生产角色由基础设施子项目管理）。
"""

from alembic import op

revision = "0001_organization_base"
down_revision = None
branch_labels = ("organization",)
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS organization")
    op.execute(
        """
        CREATE TABLE organization.org_edge (
            account_id TEXT PRIMARY KEY,
            superior_id TEXT,
            kind TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_organization_org_edge_account_id
                CHECK (length(btrim(account_id)) > 0),
            CONSTRAINT ck_organization_org_edge_kind
                CHECK (kind IN ('MANAGER', 'LEADER', 'MEMBER')),
            CONSTRAINT ck_organization_org_edge_level
                CHECK (
                    (kind = 'MANAGER' AND superior_id IS NULL)
                    OR (kind IN ('LEADER', 'MEMBER') AND superior_id IS NOT NULL)
                ),
            CONSTRAINT ck_organization_org_edge_not_self
                CHECK (superior_id IS NULL OR superior_id <> account_id),
            CONSTRAINT ck_organization_org_edge_timestamps
                CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_organization_org_edge_superior "
        "ON organization.org_edge (superior_id, kind, account_id)"
    )
    op.execute(
        """
        CREATE TABLE organization.idempotency_record (
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
            CONSTRAINT uq_organization_idempotency_scope
                UNIQUE (actor, operation, idempotency_key),
            CONSTRAINT ck_organization_idempotency_actor CHECK (length(actor) > 0),
            CONSTRAINT ck_organization_idempotency_operation CHECK (length(operation) > 0),
            CONSTRAINT ck_organization_idempotency_key CHECK (length(idempotency_key) > 0),
            CONSTRAINT ck_organization_idempotency_fingerprint
                CHECK (length(request_fingerprint) > 0),
            CONSTRAINT ck_organization_idempotency_state
                CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
            CONSTRAINT ck_organization_idempotency_result
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
            CONSTRAINT ck_organization_idempotency_timestamps
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
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'organization_rw') THEN
                CREATE ROLE organization_rw LOGIN PASSWORD 'localdev';
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA organization TO organization_rw")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON organization.org_edge, "
        "organization.idempotency_record TO organization_rw"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA organization FROM organization_rw")
    op.execute("REVOKE USAGE ON SCHEMA organization FROM organization_rw")
    op.execute("DROP TABLE organization.idempotency_record")
    op.execute("DROP TABLE organization.org_edge")
    op.execute("DROP SCHEMA organization")
