"""track source transaction outcome for safe convergence ordering."""

from alembic import op

revision = "0003_authorization_source_xid"
down_revision = "0002_authorization_convergence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE "authorization".convergence_work ADD COLUMN source_transaction_id TEXT')
    op.execute(
        'ALTER TABLE "authorization".convergence_work '
        "DROP CONSTRAINT ck_authorization_convergence_text"
    )
    op.execute(
        'ALTER TABLE "authorization".convergence_work '
        "ADD CONSTRAINT ck_authorization_convergence_text CHECK ("
        "length(btrim(source_module)) > 0 AND length(btrim(actor)) > 0 "
        "AND length(btrim(operation)) > 0 "
        "AND length(btrim(idempotency_key)) > 0 AND length(btrim(reason)) > 0 "
        "AND (source_transaction_id IS NULL "
        "OR source_transaction_id ~ '^[0-9]+$'))"
    )


def downgrade() -> None:
    op.execute(
        'ALTER TABLE "authorization".convergence_work '
        "DROP CONSTRAINT ck_authorization_convergence_text"
    )
    op.execute('ALTER TABLE "authorization".convergence_work DROP COLUMN source_transaction_id')
    op.execute(
        'ALTER TABLE "authorization".convergence_work '
        "ADD CONSTRAINT ck_authorization_convergence_text CHECK ("
        "length(btrim(source_module)) > 0 AND length(btrim(actor)) > 0 "
        "AND length(btrim(operation)) > 0 "
        "AND length(btrim(idempotency_key)) > 0 AND length(btrim(reason)) > 0)"
    )
