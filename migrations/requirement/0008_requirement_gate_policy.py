"""Requirement Gate policy owner storage and explicit audited SYSTEM_SEED."""

import hashlib
import json

from alembic import op
from sqlalchemy import text

revision = "0008_req_gate_policy"
down_revision = "0007_req_formal_delivery"
branch_labels = None
depends_on = "0002_audit_transactional_append"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE requirement.gate_policy_draft (
            id UUID PRIMARY KEY,
            namespace TEXT NOT NULL CHECK (namespace='requirement.gate'),
            scope TEXT NOT NULL CHECK (scope='PLATFORM'),
            content JSONB NOT NULL CHECK (jsonb_typeof(content)='object'),
            base_version BIGINT NOT NULL CHECK (base_version>0),
            owner_id TEXT NOT NULL CHECK (length(btrim(owner_id))>0),
            revision INTEGER NOT NULL CHECK (revision>0),
            status TEXT NOT NULL CHECK (status IN ('DRAFT','ARCHIVED')),
            stale BOOLEAN NOT NULL,
            last_meaningful_activity_at TIMESTAMPTZ NOT NULL,
            archived_at TIMESTAMPTZ,
            schema_revision INTEGER NOT NULL CHECK (schema_revision>0),
            content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            validation_evidence JSONB,
            preview_evidence JSONB,
            rollback_from_version BIGINT CHECK (rollback_from_version>0),
            CHECK ((status='DRAFT' AND archived_at IS NULL) OR
                (status='ARCHIVED' AND archived_at>=last_meaningful_activity_at))
        )
    """)
    op.execute("""
        CREATE TABLE requirement.gate_policy_receipt (
            receipt_id UUID PRIMARY KEY,
            operation TEXT NOT NULL CHECK (operation IN ('POLICY_PUBLISH','POLICY_ROLLBACK')),
            draft_id UUID NOT NULL REFERENCES requirement.gate_policy_draft(id),
            binding JSONB NOT NULL CHECK (jsonb_typeof(binding)='object'),
            referenced_at TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE requirement.gate_policy_version (
            namespace TEXT NOT NULL CHECK (namespace='requirement.gate'),
            scope TEXT NOT NULL CHECK (scope='PLATFORM'),
            version BIGINT NOT NULL CHECK (version>0),
            snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
            snapshot_hash TEXT NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
            schema_revision INTEGER NOT NULL CHECK (schema_revision>0),
            published_by TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (length(btrim(reason))>0),
            published_at TIMESTAMPTZ NOT NULL,
            activated_at TIMESTAMPTZ NOT NULL,
            source_draft_id UUID REFERENCES requirement.gate_policy_draft(id),
            receipt_id UUID UNIQUE REFERENCES requirement.gate_policy_receipt(receipt_id),
            validation_evidence JSONB NOT NULL,
            preview_evidence JSONB NOT NULL,
            dependency_versions JSONB NOT NULL,
            PRIMARY KEY (namespace,scope,version),
            CHECK ((version=1 AND published_by='SYSTEM_SEED' AND receipt_id IS NULL)
                OR (version>1 AND receipt_id IS NOT NULL AND source_draft_id IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE TABLE requirement.gate_policy_active_pointer (
            namespace TEXT NOT NULL CHECK (namespace='requirement.gate'),
            scope TEXT NOT NULL CHECK (scope='PLATFORM'),
            version BIGINT NOT NULL CHECK (version>0),
            PRIMARY KEY (namespace,scope),
            FOREIGN KEY (namespace,scope,version)
                REFERENCES requirement.gate_policy_version(namespace,scope,version)
        )
    """)
    op.execute("""
        CREATE TABLE requirement.gate_policy_outbox (
            id UUID PRIMARY KEY, namespace TEXT NOT NULL CHECK (namespace='requirement.gate'),
            scope TEXT NOT NULL CHECK (scope='PLATFORM'), event_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL, payload JSONB NOT NULL, occurred_at TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE requirement.gate_policy_idempotency (
            id UUID PRIMARY KEY, actor TEXT NOT NULL, operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('IN_PROGRESS','COMPLETED')),
            created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ, http_status INTEGER, result_metadata JSONB,
            sealed_response BYTEA, UNIQUE(actor,operation,idempotency_key)
        )
    """)
    op.execute(
        "CREATE INDEX ix_req_policy_archive ON "
        "requirement.gate_policy_draft(status,last_meaningful_activity_at,id)"
    )
    op.execute(
        "GRANT SELECT,INSERT,UPDATE ON requirement.gate_policy_draft, "
        "requirement.gate_policy_idempotency TO requirement_rw"
    )
    op.execute(
        "GRANT SELECT,INSERT ON requirement.gate_policy_version, requirement.gate_policy_receipt, "
        "requirement.gate_policy_outbox TO requirement_rw"
    )
    op.execute("GRANT SELECT ON requirement.gate_policy_active_pointer TO requirement_rw")
    op.execute("GRANT UPDATE(version) ON requirement.gate_policy_active_pointer TO requirement_rw")
    values = {
        "acceptance.additional_required_capabilities": [],
        "formal_review.additional_required_capabilities": [],
        "draft_archive_after_days": 30,
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    op.get_bind().execute(
        text("""
        INSERT INTO requirement.gate_policy_version
        (namespace,scope,version,snapshot,snapshot_hash,schema_revision,published_by,
        reason,published_at,activated_at,validation_evidence,preview_evidence,dependency_versions)
        VALUES ('requirement.gate','PLATFORM',1,CAST(:values AS JSONB),:hash,1,'SYSTEM_SEED',
        'Explicit Gate policy initialization',transaction_timestamp(),transaction_timestamp(),
        '{"valid": true,"source":"SYSTEM_SEED"}', '{"source":"SYSTEM_SEED"}', '{}')
    """),
        {"values": encoded, "hash": digest},
    )
    op.execute(
        "INSERT INTO requirement.gate_policy_active_pointer VALUES "
        "('requirement.gate','PLATFORM',1)"
    )
    op.get_bind().execute(
        text("""
        SELECT audit.append_event('configuration-system-seed-requirement-gate-v1',
        transaction_timestamp(),'SYSTEM_SEED','system','configuration.policy.seeded',
        'policy_namespace','requirement.gate','SUCCESS',:reason,
        'configuration-system-seed-requirement-gate-v1',1)
    """),
        {"reason": f"namespace=requirement.gate; version=1; snapshotHash={digest}"},
    )


def downgrade() -> None:
    for table in (
        "gate_policy_idempotency",
        "gate_policy_outbox",
        "gate_policy_active_pointer",
        "gate_policy_version",
        "gate_policy_receipt",
        "gate_policy_draft",
    ):
        op.execute(f"DROP TABLE requirement.{table}")
