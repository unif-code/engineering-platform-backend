"""Append-only captured Draft revisions, without historical backfill."""

from alembic import op
from sqlalchemy import text

revision = "0015_identity_draft_revisions"
down_revision = "0014_identity_archive_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE identity.draft_revision (
            draft_id UUID NOT NULL REFERENCES identity.draft(id),
            namespace TEXT NOT NULL CHECK (namespace='identity'),
            scope TEXT NOT NULL CHECK (scope='PLATFORM'),
            revision INTEGER NOT NULL CHECK (revision>0),
            operation TEXT NOT NULL CHECK (operation IN
                ('CREATE','UPDATE','VALIDATE','TAKEOVER','REBASE','CLONE','ROLLBACK')),
            actor_id TEXT NOT NULL CHECK (length(actor_id)>0),
            recorded_at TIMESTAMPTZ NOT NULL,
            owner_id TEXT NOT NULL CHECK (length(owner_id)>0),
            base_version BIGINT NOT NULL CHECK (base_version>0),
            schema_revision INTEGER NOT NULL CHECK (schema_revision>0),
            content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            content_json TEXT NOT NULL,
            content_hash_matches_content BOOLEAN NOT NULL,
            revision_snapshot_hash TEXT NOT NULL CHECK (revision_snapshot_hash ~ '^[0-9a-f]{64}$'),
            rollback_from_version BIGINT CHECK (rollback_from_version>0),
            validation JSONB,
            takeover JSONB,
            governance_record_id UUID,
            PRIMARY KEY (draft_id,revision)
        )
    """)
    op.execute("GRANT SELECT, INSERT ON identity.draft_revision TO identity_rw")


def downgrade() -> None:
    op.execute("LOCK TABLE identity.draft_revision IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(text("SELECT EXISTS (SELECT 1 FROM identity.draft_revision)"))
        .scalar_one()
    ):
        raise RuntimeError("Cannot downgrade while captured Draft revisions exist")
    op.execute("DROP TABLE identity.draft_revision")
