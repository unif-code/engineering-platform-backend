"""durable authorization convergence and NULL-proof state constraints."""

from alembic import op

revision = "0002_authorization_convergence"
down_revision = "0001_authorization_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'ALTER TABLE "authorization"."grant" DROP CONSTRAINT ck_authorization_grant_scope_shape'
    )
    op.execute(
        'ALTER TABLE "authorization"."grant" '
        "ADD CONSTRAINT ck_authorization_grant_scope_shape CHECK ("
        "(scope_type = 'PLATFORM' AND scope_id IS NULL) OR "
        "(scope_type = 'WORKSPACE' AND scope_id IS NOT NULL "
        "AND length(btrim(scope_id)) > 0))"
    )
    op.execute(
        'ALTER TABLE "authorization"."grant" DROP CONSTRAINT ck_authorization_grant_revocation'
    )
    op.execute(
        'ALTER TABLE "authorization"."grant" '
        "ADD CONSTRAINT ck_authorization_grant_revocation CHECK ("
        "(status = 'ACTIVE' AND revoked_at IS NULL AND revoked_by IS NULL "
        "AND revoke_reason IS NULL) OR "
        "(status = 'REVOKED' AND revoked_at IS NOT NULL "
        "AND revoked_by IS NOT NULL AND length(btrim(revoked_by)) > 0 "
        "AND revoke_reason IS NOT NULL AND length(btrim(revoke_reason)) > 0))"
    )
    op.execute(
        'ALTER TABLE "authorization".principal_version '
        "DROP CONSTRAINT ck_authorization_dirty_generation"
    )
    op.execute(
        'ALTER TABLE "authorization".principal_version '
        "ADD CONSTRAINT ck_authorization_dirty_generation CHECK ("
        "(dirty_generation IS NULL AND dirty_reason IS NULL) OR "
        "(dirty_generation IS NOT NULL AND dirty_generation > 0 "
        "AND dirty_generation <= fence_generation AND dirty_reason IS NOT NULL "
        "AND length(btrim(dirty_reason)) > 0))"
    )
    op.execute(
        'ALTER TABLE "authorization".idempotency_record '
        "DROP CONSTRAINT ck_authorization_idempotency_result"
    )
    op.execute(
        'ALTER TABLE "authorization".idempotency_record '
        "ADD CONSTRAINT ck_authorization_idempotency_result CHECK ("
        "(state = 'IN_PROGRESS' AND http_status IS NULL "
        "AND result_metadata IS NULL AND sealed_response IS NULL "
        "AND completed_at IS NULL) OR "
        "(state = 'COMPLETED' AND http_status IS NOT NULL "
        "AND http_status BETWEEN 100 AND 599 AND result_metadata IS NOT NULL "
        "AND sealed_response IS NOT NULL AND completed_at IS NOT NULL))"
    )
    op.execute(
        """
        CREATE TABLE "authorization".convergence_work (
            id UUID PRIMARY KEY,
            source_module TEXT NOT NULL,
            actor TEXT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            generation_map JSONB NOT NULL,
            affected_account_ids JSONB NOT NULL,
            affected_workspace_ids JSONB NOT NULL,
            recompute_membership BOOLEAN NOT NULL,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            cancelled_at TIMESTAMPTZ,
            CONSTRAINT uq_authorization_convergence_source
                UNIQUE (source_module, actor, operation, idempotency_key),
            CONSTRAINT ck_authorization_convergence_text CHECK (
                length(btrim(source_module)) > 0 AND length(btrim(actor)) > 0
                AND length(btrim(operation)) > 0
                AND length(btrim(idempotency_key)) > 0
                AND length(btrim(reason)) > 0
            ),
            CONSTRAINT ck_authorization_convergence_generations CHECK (
                jsonb_typeof(generation_map) = 'object'
                AND NOT jsonb_path_exists(
                    generation_map,
                    '$.* ? (@.type() != "number" || @ <= 0)'
                )
            ),
            CONSTRAINT ck_authorization_convergence_accounts CHECK (
                jsonb_typeof(affected_account_ids) = 'array'
                AND NOT jsonb_path_exists(
                    affected_account_ids,
                    '$[*] ? (@.type() != "string" || @ == "")'
                )
            ),
            CONSTRAINT ck_authorization_convergence_workspaces CHECK (
                jsonb_typeof(affected_workspace_ids) = 'array'
                AND NOT jsonb_path_exists(
                    affected_workspace_ids,
                    '$[*] ? (@.type() != "string" || @ == "")'
                )
            ),
            CONSTRAINT ck_authorization_convergence_state CHECK (
                (status = 'PENDING'
                    AND phase IN ('SOURCE_REGISTERED', 'RECOMPUTE_PENDING', 'VERSION_PENDING')
                    AND completed_at IS NULL AND cancelled_at IS NULL)
                OR
                (status = 'COMPLETED' AND phase = 'DONE'
                    AND completed_at IS NOT NULL AND cancelled_at IS NULL)
                OR
                (status = 'CANCELLED' AND phase = 'CANCELLED'
                    AND completed_at IS NULL AND cancelled_at IS NOT NULL)
            ),
            CONSTRAINT ck_authorization_convergence_timestamps CHECK (
                updated_at >= created_at
                AND (completed_at IS NULL OR
                    (completed_at >= created_at AND completed_at <= updated_at))
                AND (cancelled_at IS NULL OR
                    (cancelled_at >= created_at AND cancelled_at <= updated_at))
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_authorization_convergence_pending "
        'ON "authorization".convergence_work (status, created_at, id)'
    )
    op.execute(
        "CREATE INDEX ix_authorization_convergence_generations "
        'ON "authorization".convergence_work USING GIN (generation_map)'
    )
    op.execute(
        'GRANT SELECT, INSERT, UPDATE ON "authorization".convergence_work TO authorization_rw'
    )


def downgrade() -> None:
    op.execute('DROP TABLE "authorization".convergence_work')
    op.execute(
        'ALTER TABLE "authorization".idempotency_record '
        "DROP CONSTRAINT ck_authorization_idempotency_result"
    )
    op.execute(
        'ALTER TABLE "authorization".idempotency_record '
        "ADD CONSTRAINT ck_authorization_idempotency_result CHECK ("
        "(state = 'IN_PROGRESS' AND http_status IS NULL "
        "AND result_metadata IS NULL AND sealed_response IS NULL "
        "AND completed_at IS NULL) OR "
        "(state = 'COMPLETED' AND http_status BETWEEN 100 AND 599 "
        "AND result_metadata IS NOT NULL AND sealed_response IS NOT NULL "
        "AND completed_at IS NOT NULL))"
    )
    op.execute(
        'ALTER TABLE "authorization".principal_version '
        "DROP CONSTRAINT ck_authorization_dirty_generation"
    )
    op.execute(
        'ALTER TABLE "authorization".principal_version '
        "ADD CONSTRAINT ck_authorization_dirty_generation CHECK ("
        "(dirty_generation IS NULL AND dirty_reason IS NULL) OR "
        "(dirty_generation IS NOT NULL AND dirty_generation > 0 "
        "AND dirty_generation <= fence_generation "
        "AND length(btrim(dirty_reason)) > 0))"
    )
    op.execute(
        'ALTER TABLE "authorization"."grant" DROP CONSTRAINT ck_authorization_grant_revocation'
    )
    op.execute(
        'ALTER TABLE "authorization"."grant" '
        "ADD CONSTRAINT ck_authorization_grant_revocation CHECK ("
        "(status = 'ACTIVE' AND revoked_at IS NULL AND revoked_by IS NULL "
        "AND revoke_reason IS NULL) OR "
        "(status = 'REVOKED' AND revoked_at IS NOT NULL "
        "AND length(btrim(revoked_by)) > 0 "
        "AND length(btrim(revoke_reason)) > 0))"
    )
    op.execute(
        'ALTER TABLE "authorization"."grant" DROP CONSTRAINT ck_authorization_grant_scope_shape'
    )
    op.execute(
        'ALTER TABLE "authorization"."grant" '
        "ADD CONSTRAINT ck_authorization_grant_scope_shape CHECK ("
        "(scope_type = 'PLATFORM' AND scope_id IS NULL) OR "
        "(scope_type = 'WORKSPACE' AND length(btrim(scope_id)) > 0))"
    )
