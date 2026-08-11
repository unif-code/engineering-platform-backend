"""normalize pending authorization convergence work per principal."""

from alembic import op

revision = "0004_authorization_pending_set"
down_revision = "0003_authorization_source_xid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'ALTER TABLE "authorization".convergence_work '
        "ADD COLUMN idempotency_claim_id UUID, ADD COLUMN request_fingerprint TEXT"
    )
    op.execute(
        'ALTER TABLE "authorization".convergence_work '
        "DROP CONSTRAINT IF EXISTS uq_authorization_convergence_source"
    )
    op.execute(
        'ALTER TABLE "authorization".convergence_work '
        "ADD CONSTRAINT ck_authorization_convergence_claim_binding CHECK ("
        "(idempotency_claim_id IS NULL AND request_fingerprint IS NULL) OR "
        "(idempotency_claim_id IS NOT NULL AND request_fingerprint IS NOT NULL "
        "AND length(btrim(request_fingerprint)) > 0))"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_authorization_convergence_claim "
        'ON "authorization".convergence_work (source_module, idempotency_claim_id) '
        "WHERE idempotency_claim_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE "authorization".convergence_principal_pending (
            work_id UUID NOT NULL REFERENCES "authorization".convergence_work(id),
            account_id TEXT NOT NULL,
            generation BIGINT NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (work_id, account_id),
            CONSTRAINT uq_authorization_pending_principal_generation
                UNIQUE (account_id, generation),
            CONSTRAINT ck_authorization_pending_account CHECK (
                length(btrim(account_id)) > 0
            ),
            CONSTRAINT ck_authorization_pending_generation CHECK (generation > 0),
            CONSTRAINT ck_authorization_pending_reason CHECK (
                length(btrim(reason)) > 0
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO "authorization".convergence_principal_pending
            (work_id, account_id, generation, reason, created_at)
        SELECT work.id, generation.key, generation.value::text::bigint,
               work.reason, work.created_at
        FROM "authorization".convergence_work AS work
        CROSS JOIN LATERAL jsonb_each(work.generation_map) AS generation
        WHERE work.status = 'PENDING'
        """
    )
    op.execute(
        "CREATE INDEX ix_authorization_pending_principal "
        'ON "authorization".convergence_principal_pending '
        "(account_id, generation DESC, work_id)"
    )
    op.execute(
        'GRANT SELECT, INSERT, DELETE ON "authorization".convergence_principal_pending '
        "TO authorization_rw"
    )


def downgrade() -> None:
    op.execute('DROP TABLE "authorization".convergence_principal_pending')
    op.execute('DROP INDEX IF EXISTS "authorization".uq_authorization_convergence_claim')
    op.execute(
        'ALTER TABLE "authorization".convergence_work '
        "DROP CONSTRAINT IF EXISTS ck_authorization_convergence_claim_binding"
    )
    op.execute(
        'ALTER TABLE "authorization".convergence_work '
        "DROP COLUMN IF EXISTS request_fingerprint, "
        "DROP COLUMN IF EXISTS idempotency_claim_id"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_authorization_convergence_source'
                  AND conrelid = '"authorization".convergence_work'::regclass
            ) AND NOT EXISTS (
                SELECT 1
                FROM "authorization".convergence_work
                GROUP BY source_module, actor, operation, idempotency_key
                HAVING count(*) > 1
            ) THEN
                ALTER TABLE "authorization".convergence_work
                    ADD CONSTRAINT uq_authorization_convergence_source
                    UNIQUE (source_module, actor, operation, idempotency_key);
            END IF;
        END
        $migration$
        """
    )
