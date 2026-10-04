"""Requirement-owned immutable records for new explicit Draft Rebase commands."""

from alembic import op
from sqlalchemy import text

revision = "0010_req_draft_rebase"
down_revision = "0009_req_formal_owner_denial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE requirement.gate_policy_rebase (
            id UUID PRIMARY KEY,
            draft_id UUID NOT NULL REFERENCES requirement.gate_policy_draft(id),
            namespace TEXT NOT NULL CHECK (namespace='requirement.gate'),
            scope TEXT NOT NULL CHECK (scope='PLATFORM'),
            schema_revision INTEGER NOT NULL CHECK (schema_revision>0),
            actor_id UUID NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL,
            before_revision BIGINT NOT NULL CHECK (before_revision>0),
            after_revision BIGINT NOT NULL CHECK (after_revision=before_revision+1),
            base_version BIGINT NOT NULL CHECK (base_version>0),
            current_version BIGINT NOT NULL CHECK (current_version>base_version),
            base_snapshot_hash TEXT NOT NULL CHECK (base_snapshot_hash ~ '^[0-9a-f]{64}$'),
            current_snapshot_hash TEXT NOT NULL CHECK (current_snapshot_hash ~ '^[0-9a-f]{64}$'),
            before_content_hash TEXT NOT NULL CHECK (before_content_hash ~ '^[0-9a-f]{64}$'),
            after_content_hash TEXT NOT NULL CHECK (after_content_hash ~ '^[0-9a-f]{64}$'),
            before_content JSONB NOT NULL CHECK (jsonb_typeof(before_content)='object'),
            after_content JSONB NOT NULL CHECK (jsonb_typeof(after_content)='object'),
            selections JSONB NOT NULL CHECK (jsonb_typeof(selections)='object'),
            UNIQUE (draft_id,after_revision),
            FOREIGN KEY (namespace,scope,base_version)
                REFERENCES requirement.gate_policy_version(namespace,scope,version),
            FOREIGN KEY (namespace,scope,current_version)
                REFERENCES requirement.gate_policy_version(namespace,scope,version)
        )
    """)
    op.execute("GRANT SELECT, INSERT ON requirement.gate_policy_rebase TO requirement_rw")


def downgrade() -> None:
    op.execute("LOCK TABLE requirement.gate_policy_rebase IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(text("SELECT EXISTS (SELECT 1 FROM requirement.gate_policy_rebase)"))
        .scalar_one()
    ):
        raise RuntimeError("Cannot downgrade while Draft Rebase records exist")
    op.execute("DROP TABLE requirement.gate_policy_rebase")
