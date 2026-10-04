"""Requirement-owned immutable provenance for explicit Draft Clone."""

from alembic import op
from sqlalchemy import text

revision = "0011_req_draft_clone"
down_revision = "0010_req_draft_rebase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE requirement.gate_policy_clone (
            id UUID PRIMARY KEY,
            draft_id UUID NOT NULL UNIQUE REFERENCES requirement.gate_policy_draft(id),
            source_draft_id UUID NOT NULL REFERENCES requirement.gate_policy_draft(id),
            namespace TEXT NOT NULL CHECK (namespace='requirement.gate'),
            scope TEXT NOT NULL CHECK (scope='PLATFORM'),
            schema_revision INTEGER NOT NULL CHECK (schema_revision>0),
            actor_id UUID NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL,
            source_revision BIGINT NOT NULL CHECK (source_revision>0),
            source_owner_id UUID NOT NULL,
            source_status TEXT NOT NULL CHECK (source_status IN ('DRAFT','ARCHIVED')),
            base_version BIGINT NOT NULL CHECK (base_version>0),
            current_version BIGINT NOT NULL CHECK (current_version>=base_version),
            base_snapshot_hash TEXT NOT NULL CHECK (base_snapshot_hash ~ '^[0-9a-f]{64}$'),
            current_snapshot_hash TEXT NOT NULL CHECK (current_snapshot_hash ~ '^[0-9a-f]{64}$'),
            source_content_hash TEXT NOT NULL CHECK (source_content_hash ~ '^[0-9a-f]{64}$'),
            content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            source_content JSONB NOT NULL CHECK (jsonb_typeof(source_content)='object'),
            rollback_from_version BIGINT CHECK (rollback_from_version>0),
            cloned_from_archived_draft_id UUID REFERENCES requirement.gate_policy_draft(id),
            CHECK (draft_id<>source_draft_id),
            CHECK ((source_status='ARCHIVED' AND cloned_from_archived_draft_id IS NOT NULL
                    AND cloned_from_archived_draft_id=source_draft_id)
                   OR (source_status='DRAFT' AND cloned_from_archived_draft_id IS NULL)),
            FOREIGN KEY (namespace,scope,base_version)
                REFERENCES requirement.gate_policy_version(namespace,scope,version),
            FOREIGN KEY (namespace,scope,current_version)
                REFERENCES requirement.gate_policy_version(namespace,scope,version),
            FOREIGN KEY (namespace,scope,rollback_from_version)
                REFERENCES requirement.gate_policy_version(namespace,scope,version)
        )
    """)
    op.execute("GRANT SELECT, INSERT ON requirement.gate_policy_clone TO requirement_rw")


def downgrade() -> None:
    op.execute("LOCK TABLE requirement.gate_policy_clone IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(text("SELECT EXISTS (SELECT 1 FROM requirement.gate_policy_clone)"))
        .scalar_one()
    ):
        raise RuntimeError("Cannot downgrade while Draft Clone records exist")
    op.execute("DROP TABLE requirement.gate_policy_clone")
