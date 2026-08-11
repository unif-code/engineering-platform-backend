"""authorization baseline: grants, version fences, registry, durable commands.

The migration creates only a NOLOGIN privilege role. Infrastructure owns the
runtime login and credential lifecycle.
"""

from alembic import op

revision = "0001_authorization_base"
down_revision = None
branch_labels = ("authorization",)
depends_on = None


def upgrade() -> None:
    # AUTHORIZATION is a PostgreSQL keyword, so the schema is always quoted.
    op.execute('CREATE SCHEMA IF NOT EXISTS "authorization"')
    op.execute(
        """
        CREATE TABLE "authorization"."grant" (
            id UUID PRIMARY KEY,
            principal_id TEXT NOT NULL,
            capability TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_id TEXT,
            source TEXT NOT NULL,
            valid_from TIMESTAMPTZ,
            valid_to TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            revoked_by TEXT,
            revoke_reason TEXT,
            CONSTRAINT ck_authorization_grant_principal
                CHECK (length(btrim(principal_id)) > 0),
            CONSTRAINT ck_authorization_grant_capability
                CHECK (length(btrim(capability)) > 0),
            CONSTRAINT ck_authorization_grant_source
                CHECK (length(btrim(source)) > 0),
            CONSTRAINT ck_authorization_grant_scope_type
                CHECK (scope_type IN ('PLATFORM', 'WORKSPACE')),
            CONSTRAINT ck_authorization_grant_scope_shape
                CHECK (
                    (scope_type = 'PLATFORM' AND scope_id IS NULL)
                    OR
                    (scope_type = 'WORKSPACE' AND length(btrim(scope_id)) > 0)
                ),
            CONSTRAINT ck_authorization_grant_validity
                CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to > valid_from),
            CONSTRAINT ck_authorization_grant_status
                CHECK (status IN ('ACTIVE', 'REVOKED')),
            CONSTRAINT ck_authorization_grant_version CHECK (version >= 1),
            CONSTRAINT ck_authorization_grant_timestamps CHECK (updated_at >= created_at),
            CONSTRAINT ck_authorization_grant_revocation CHECK (
                (status = 'ACTIVE' AND revoked_at IS NULL AND revoked_by IS NULL
                    AND revoke_reason IS NULL)
                OR
                (status = 'REVOKED' AND revoked_at IS NOT NULL
                    AND length(btrim(revoked_by)) > 0
                    AND length(btrim(revoke_reason)) > 0)
            )
        )
        """
    )
    op.execute(
        'CREATE INDEX ix_authorization_grant_decision ON "authorization"."grant" '
        "(principal_id, capability, scope_type, scope_id) WHERE status='ACTIVE'"
    )
    op.execute(
        'CREATE INDEX ix_authorization_grant_principal ON "authorization"."grant" '
        "(principal_id, created_at, id)"
    )
    op.execute(
        """
        CREATE TABLE "authorization".principal_version (
            account_id TEXT PRIMARY KEY,
            version BIGINT NOT NULL DEFAULT 1,
            fence_generation BIGINT NOT NULL DEFAULT 0,
            dirty_generation BIGINT,
            dirty_reason TEXT,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT ck_authorization_principal_account
                CHECK (length(btrim(account_id)) > 0),
            CONSTRAINT ck_authorization_principal_version CHECK (version >= 1),
            CONSTRAINT ck_authorization_fence_generation CHECK (fence_generation >= 0),
            CONSTRAINT ck_authorization_dirty_generation CHECK (
                (dirty_generation IS NULL AND dirty_reason IS NULL)
                OR
                (dirty_generation IS NOT NULL AND dirty_generation > 0
                    AND dirty_generation <= fence_generation
                    AND length(btrim(dirty_reason)) > 0)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_authorization_principal_dirty "
        'ON "authorization".principal_version (account_id, dirty_generation) '
        "WHERE dirty_generation IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE "authorization".route_registry (
            route_key TEXT PRIMARY KEY,
            capability TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            sort INTEGER NOT NULL,
            meta JSONB NOT NULL,
            CONSTRAINT ck_authorization_route_key CHECK (length(btrim(route_key)) > 0),
            CONSTRAINT ck_authorization_route_capability
                CHECK (length(btrim(capability)) > 0),
            CONSTRAINT ck_authorization_route_scope CHECK (scope_type = 'PLATFORM'),
            CONSTRAINT ck_authorization_route_sort CHECK (sort >= 0),
            CONSTRAINT ck_authorization_route_meta CHECK (jsonb_typeof(meta) = 'object')
        )
        """
    )
    op.execute(
        """
        INSERT INTO "authorization".route_registry
            (route_key, capability, scope_type, sort, meta)
        VALUES
            ('home', 'platform.home.read', 'PLATFORM', 1,
                jsonb_build_object('name', '首页', 'order', 1)),
            ('admin', 'platform.admin.access', 'PLATFORM', 2,
                jsonb_build_object('name', '管理后台', 'order', 2))
        """
    )
    op.execute(
        """
        CREATE TABLE "authorization".idempotency_record (
            id UUID PRIMARY KEY,
            actor TEXT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            http_status INTEGER,
            result_metadata JSONB,
            sealed_response BYTEA,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            CONSTRAINT uq_authorization_idempotency_scope
                UNIQUE (actor, operation, idempotency_key),
            CONSTRAINT ck_authorization_idempotency_text CHECK (
                length(actor) > 0 AND length(operation) > 0
                AND length(idempotency_key) > 0 AND length(request_fingerprint) > 0
            ),
            CONSTRAINT ck_authorization_idempotency_state
                CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
            CONSTRAINT ck_authorization_idempotency_result CHECK (
                (state = 'IN_PROGRESS' AND http_status IS NULL
                    AND result_metadata IS NULL AND sealed_response IS NULL
                    AND completed_at IS NULL)
                OR
                (state = 'COMPLETED' AND http_status BETWEEN 100 AND 599
                    AND result_metadata IS NOT NULL AND sealed_response IS NOT NULL
                    AND completed_at IS NOT NULL)
            ),
            CONSTRAINT ck_authorization_idempotency_timestamps CHECK (
                updated_at >= created_at
                AND (completed_at IS NULL OR
                    (completed_at >= created_at AND completed_at <= updated_at))
            )
        )
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authorization_rw') THEN
                CREATE ROLE authorization_rw NOLOGIN;
            END IF;
        END $$
        """
    )
    op.execute('GRANT USAGE ON SCHEMA "authorization" TO authorization_rw')
    op.execute(
        'GRANT SELECT, INSERT, UPDATE ON "authorization"."grant", '
        '"authorization".principal_version, "authorization".idempotency_record '
        "TO authorization_rw"
    )
    op.execute('GRANT SELECT ON "authorization".route_registry TO authorization_rw')


def downgrade() -> None:
    op.execute('REVOKE ALL ON ALL TABLES IN SCHEMA "authorization" FROM authorization_rw')
    op.execute('REVOKE USAGE ON SCHEMA "authorization" FROM authorization_rw')
    op.execute('DROP TABLE "authorization".idempotency_record')
    op.execute('DROP TABLE "authorization".route_registry')
    op.execute('DROP TABLE "authorization".principal_version')
    op.execute('DROP TABLE "authorization"."grant"')
    op.execute('DROP SCHEMA "authorization"')
