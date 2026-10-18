"""Append-only captured Draft revisions, without historical backfill."""

from alembic import op
from sqlalchemy import text

revision = "0013_req_draft_revisions"
down_revision = "0012_req_archive_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE requirement.gate_policy_revision (
            draft_id UUID NOT NULL REFERENCES requirement.gate_policy_draft(id),
            namespace TEXT NOT NULL CHECK (namespace='requirement.gate'),
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
    op.execute("GRANT SELECT, INSERT ON requirement.gate_policy_revision TO requirement_rw")


def downgrade() -> None:
    op.execute("LOCK TABLE requirement.gate_policy_revision IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(text("SELECT EXISTS (SELECT 1 FROM requirement.gate_policy_revision)"))
        .scalar_one()
    ):
        raise RuntimeError("Cannot downgrade while captured Draft revisions exist")
    op.execute("DROP TABLE requirement.gate_policy_revision")
