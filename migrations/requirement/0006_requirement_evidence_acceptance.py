"""Add immutable delivery snapshots, selections, and V0.6 delivery gates."""

import hashlib
import json
from typing import cast

from alembic import op
from sqlalchemy import text

revision = "0006_req_evidence_acceptance"
down_revision = "0005_req_sdd_human_gate"
branch_labels = None
depends_on = None


def _acceptance_criteria_hash(raw: object) -> str:
    criteria = cast(list[object], raw)
    normalized = [str(value).strip() for value in criteria]
    canonical = json.dumps(
        {"acceptanceCriteria": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def upgrade() -> None:
    op.execute(
        "ALTER TABLE requirement.requirement "
        "ADD COLUMN acceptance_criteria_version INTEGER NOT NULL DEFAULT 1, "
        "ADD COLUMN acceptance_criteria_hash TEXT, "
        "ADD COLUMN current_integration_baseline_selection_id UUID, "
        "ADD COLUMN current_acceptance_gate_id UUID"
    )
    connection = op.get_bind()
    rows = connection.execute(
        text("SELECT id, acceptance_criteria FROM requirement.requirement ORDER BY id")
    ).mappings()
    for row in rows:
        connection.execute(
            text(
                "UPDATE requirement.requirement SET acceptance_criteria_hash=:criteria_hash "
                "WHERE id=:requirement_id"
            ),
            {
                "requirement_id": row["id"],
                "criteria_hash": _acceptance_criteria_hash(row["acceptance_criteria"]),
            },
        )
    op.execute(
        "ALTER TABLE requirement.requirement "
        "ALTER COLUMN acceptance_criteria_version DROP DEFAULT, "
        "ALTER COLUMN acceptance_criteria_hash SET NOT NULL, "
        "ADD CONSTRAINT ck_req_acceptance_criteria_version_hash CHECK ("
        "acceptance_criteria_version >= 1 "
        "AND acceptance_criteria_hash ~ '^sha256:[0-9a-f]{64}$')"
    )

    op.execute("ALTER TABLE requirement.requirement DROP CONSTRAINT ck_requirement_state")
    op.execute(
        """
        ALTER TABLE requirement.requirement
        ADD CONSTRAINT ck_requirement_state CHECK (
            state IN (
                'CREATED', 'PREPARING', 'AWAITING_CONFIRMATION', 'READY',
                'IN_PROGRESS', 'VERIFYING', 'AWAITING_ACCEPTANCE',
                'AWAITING_MERGE', 'COMPLETED', 'CANCELED'
            )
        )
        """
    )
    op.execute("ALTER TABLE requirement.work_item DROP CONSTRAINT ck_requirement_work_item_state")
    op.execute(
        """
        ALTER TABLE requirement.work_item
        ADD CONSTRAINT ck_requirement_work_item_state CHECK (
            state = 'DRAFT'
            OR (
                state IN ('READY', 'IN_PROGRESS', 'VERIFYING', 'AWAITING_MERGE', 'COMPLETED')
                AND assignment_state = 'ASSIGNED'
                AND repository_state = 'BOUND'
            )
            OR state = 'CANCELED'
        )
        """
    )
    op.execute(
        "ALTER TABLE requirement.work_item "
        "ADD CONSTRAINT uq_req_work_item_owner UNIQUE (id, requirement_id)"
    )

    op.execute(
        """
        CREATE TABLE requirement.requirement_delivery_snapshot (
            id UUID PRIMARY KEY,
            requirement_id UUID NOT NULL,
            requirement_version INTEGER NOT NULL,
            required_work_item_set_version INTEGER NOT NULL,
            required_work_item_set_hash TEXT NOT NULL,
            work_item_ids JSONB NOT NULL,
            snapshot_hash TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_req_snapshot_requirement
                FOREIGN KEY (requirement_id) REFERENCES requirement.requirement(id),
            CONSTRAINT uq_req_snapshot_owner UNIQUE (id, requirement_id),
            CONSTRAINT uq_req_snapshot_exact_subject UNIQUE (
                id,
                requirement_id,
                requirement_version,
                required_work_item_set_version,
                required_work_item_set_hash,
                snapshot_hash
            ),
            CONSTRAINT uq_req_snapshot_hash UNIQUE (requirement_id, snapshot_hash),
            CONSTRAINT ck_req_snapshot_versions CHECK (
                requirement_version >= 1 AND required_work_item_set_version >= 1
            ),
            CONSTRAINT ck_req_snapshot_hashes CHECK (
                required_work_item_set_hash ~ '^sha256:[0-9a-f]{64}$'
                AND snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_req_snapshot_items CHECK (
                jsonb_typeof(work_item_ids) = 'array'
                AND jsonb_array_length(work_item_ids) >= 1
            ),
            CONSTRAINT ck_req_snapshot_creator CHECK (length(btrim(created_by)) > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE requirement.integration_baseline_selection (
            id UUID PRIMARY KEY,
            requirement_id UUID NOT NULL,
            delivery_snapshot_id UUID NOT NULL,
            delivery_snapshot_hash TEXT NOT NULL,
            integration_baseline_id UUID NOT NULL,
            integration_baseline_hash TEXT NOT NULL,
            evidence_requirement_version INTEGER NOT NULL,
            evidence_required_work_item_set_version INTEGER NOT NULL,
            evidence_required_work_item_set_hash TEXT NOT NULL,
            requirement_version_before INTEGER NOT NULL,
            requirement_version_after INTEGER NOT NULL,
            selected_by TEXT NOT NULL,
            selected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            invalidated_at TIMESTAMPTZ,
            invalidation_reason TEXT,
            CONSTRAINT fk_req_selection_requirement
                FOREIGN KEY (requirement_id) REFERENCES requirement.requirement(id),
            CONSTRAINT fk_req_selection_snapshot FOREIGN KEY (
                delivery_snapshot_id,
                requirement_id,
                evidence_requirement_version,
                evidence_required_work_item_set_version,
                evidence_required_work_item_set_hash,
                delivery_snapshot_hash
            ) REFERENCES requirement.requirement_delivery_snapshot (
                id,
                requirement_id,
                requirement_version,
                required_work_item_set_version,
                required_work_item_set_hash,
                snapshot_hash
            ),
            CONSTRAINT uq_req_selection_owner UNIQUE (id, requirement_id),
            CONSTRAINT uq_req_selection_exact_subject UNIQUE (
                id,
                requirement_id,
                requirement_version_after,
                integration_baseline_id,
                integration_baseline_hash
            ),
            CONSTRAINT uq_req_selection_evidence UNIQUE (
                requirement_id, integration_baseline_id, integration_baseline_hash
            ),
            CONSTRAINT uq_req_selection_version UNIQUE (
                requirement_id, requirement_version_after
            ),
            CONSTRAINT ck_req_selection_versions CHECK (
                evidence_requirement_version >= 1
                AND evidence_required_work_item_set_version >= 1
                AND requirement_version_before >= 1
                AND requirement_version_before = evidence_requirement_version
                AND requirement_version_after = requirement_version_before + 1
            ),
            CONSTRAINT ck_req_selection_hashes CHECK (
                delivery_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
                AND integration_baseline_hash ~ '^sha256:[0-9a-f]{64}$'
                AND evidence_required_work_item_set_hash ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_req_selection_actor CHECK (length(btrim(selected_by)) > 0),
            CONSTRAINT ck_req_selection_invalidation CHECK (
                (invalidated_at IS NULL AND invalidation_reason IS NULL)
                OR (
                    invalidated_at IS NOT NULL
                    AND length(btrim(invalidation_reason)) > 0
                    AND invalidated_at >= selected_at
                )
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_req_current_selection "
        "ON requirement.integration_baseline_selection (requirement_id) "
        "WHERE invalidated_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE requirement.delivery_gate (
            id UUID PRIMARY KEY,
            gate_type TEXT NOT NULL,
            requirement_id UUID NOT NULL,
            work_item_id UUID,
            selection_id UUID NOT NULL,
            requirement_version INTEGER NOT NULL,
            acceptance_criteria_version INTEGER NOT NULL,
            acceptance_criteria_hash TEXT NOT NULL,
            integration_baseline_id UUID NOT NULL,
            integration_baseline_hash TEXT NOT NULL,
            formal_merge_request_binding_id UUID,
            subject_head_sha TEXT,
            policy_code TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            policy_snapshot_hash TEXT NOT NULL,
            state TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            decided_at TIMESTAMPTZ,
            invalidated_at TIMESTAMPTZ,
            invalidation_reason TEXT,
            CONSTRAINT fk_req_delivery_gate_requirement
                FOREIGN KEY (requirement_id) REFERENCES requirement.requirement(id),
            CONSTRAINT fk_req_delivery_gate_selection FOREIGN KEY (
                selection_id,
                requirement_id,
                requirement_version,
                integration_baseline_id,
                integration_baseline_hash
            ) REFERENCES requirement.integration_baseline_selection (
                id,
                requirement_id,
                requirement_version_after,
                integration_baseline_id,
                integration_baseline_hash
            ),
            CONSTRAINT fk_req_delivery_gate_work_item FOREIGN KEY (
                work_item_id, requirement_id
            ) REFERENCES requirement.work_item (id, requirement_id),
            CONSTRAINT uq_req_delivery_gate_owner UNIQUE (id, requirement_id),
            CONSTRAINT ck_req_delivery_gate_type CHECK (
                gate_type IN ('REQUIREMENT_ACCEPTANCE', 'FORMAL_MR_REVIEW')
            ),
            CONSTRAINT ck_req_delivery_gate_subject CHECK (
                (
                    gate_type = 'REQUIREMENT_ACCEPTANCE'
                    AND work_item_id IS NULL
                    AND formal_merge_request_binding_id IS NULL
                    AND subject_head_sha IS NULL
                )
                OR (
                    gate_type = 'FORMAL_MR_REVIEW'
                    AND work_item_id IS NOT NULL
                    AND formal_merge_request_binding_id IS NOT NULL
                    AND subject_head_sha ~ '^[0-9a-f]{40}$'
                )
            ),
            CONSTRAINT ck_req_delivery_gate_versions CHECK (
                requirement_version >= 1
                AND acceptance_criteria_version >= 1
                AND policy_version >= 1
                AND revision >= 1
            ),
            CONSTRAINT ck_req_delivery_gate_hashes CHECK (
                acceptance_criteria_hash ~ '^sha256:[0-9a-f]{64}$'
                AND integration_baseline_hash ~ '^sha256:[0-9a-f]{64}$'
                AND policy_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_req_delivery_gate_policy CHECK (
                length(btrim(policy_code)) > 0
            ),
            CONSTRAINT ck_req_delivery_gate_state CHECK (
                (
                    state = 'OPEN'
                    AND decided_at IS NULL
                    AND invalidated_at IS NULL
                    AND invalidation_reason IS NULL
                )
                OR (
                    state = 'DECIDED'
                    AND decided_at IS NOT NULL
                    AND invalidated_at IS NULL
                    AND invalidation_reason IS NULL
                )
                OR (
                    state = 'INVALIDATED'
                    AND invalidated_at IS NOT NULL
                    AND length(btrim(invalidation_reason)) > 0
                )
            ),
            CONSTRAINT ck_req_delivery_gate_times CHECK (
                (decided_at IS NULL OR decided_at >= created_at)
                AND (invalidated_at IS NULL OR invalidated_at >= created_at)
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_req_acceptance_gate_selection "
        "ON requirement.delivery_gate (selection_id) "
        "WHERE gate_type='REQUIREMENT_ACCEPTANCE'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_req_formal_gate_binding "
        "ON requirement.delivery_gate (formal_merge_request_binding_id, selection_id) "
        "WHERE gate_type='FORMAL_MR_REVIEW'"
    )

    op.execute(
        """
        CREATE TABLE requirement.delivery_gate_assignment (
            id UUID PRIMARY KEY,
            gate_id UUID NOT NULL,
            default_reviewer_id TEXT NOT NULL,
            current_reviewer_id TEXT NOT NULL,
            resolution_snapshot JSONB NOT NULL,
            revision INTEGER NOT NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            superseded_at TIMESTAMPTZ,
            CONSTRAINT fk_req_delivery_assignment_gate
                FOREIGN KEY (gate_id) REFERENCES requirement.delivery_gate(id),
            CONSTRAINT uq_req_delivery_assignment_owner UNIQUE (id, gate_id),
            CONSTRAINT uq_req_delivery_assignment_revision UNIQUE (gate_id, revision),
            CONSTRAINT ck_req_delivery_assignment_reviewers CHECK (
                length(btrim(default_reviewer_id)) > 0
                AND length(btrim(current_reviewer_id)) > 0
            ),
            CONSTRAINT ck_req_delivery_assignment_snapshot CHECK (
                jsonb_typeof(resolution_snapshot) = 'object'
            ),
            CONSTRAINT ck_req_delivery_assignment_revision CHECK (revision >= 1),
            CONSTRAINT ck_req_delivery_assignment_times CHECK (
                superseded_at IS NULL OR superseded_at >= assigned_at
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_req_current_delivery_assignment "
        "ON requirement.delivery_gate_assignment (gate_id) WHERE superseded_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE requirement.delivery_decision (
            id UUID PRIMARY KEY,
            gate_id UUID NOT NULL,
            gate_assignment_id UUID NOT NULL,
            reviewer_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL,
            subject_revision INTEGER NOT NULL,
            requirement_version INTEGER NOT NULL,
            acceptance_criteria_version INTEGER NOT NULL,
            acceptance_criteria_hash TEXT NOT NULL,
            integration_baseline_id UUID NOT NULL,
            integration_baseline_hash TEXT NOT NULL,
            subject_head_sha TEXT,
            eligibility_snapshot JSONB NOT NULL,
            validity TEXT NOT NULL,
            decided_at TIMESTAMPTZ NOT NULL,
            invalidated_at TIMESTAMPTZ,
            invalidation_reason TEXT,
            CONSTRAINT fk_req_delivery_decision_gate
                FOREIGN KEY (gate_id) REFERENCES requirement.delivery_gate(id),
            CONSTRAINT fk_req_delivery_decision_assignment FOREIGN KEY (
                gate_assignment_id, gate_id
            ) REFERENCES requirement.delivery_gate_assignment (id, gate_id),
            CONSTRAINT uq_req_delivery_decision_gate UNIQUE (gate_id),
            CONSTRAINT ck_req_delivery_decision_actor CHECK (
                length(btrim(reviewer_id)) > 0 AND length(btrim(reason)) > 0
            ),
            CONSTRAINT ck_req_delivery_decision_outcome CHECK (
                outcome IN ('APPROVED', 'CHANGES_REQUESTED', 'REJECTED')
            ),
            CONSTRAINT ck_req_delivery_decision_versions CHECK (
                subject_revision >= 1
                AND requirement_version >= 1
                AND acceptance_criteria_version >= 1
            ),
            CONSTRAINT ck_req_delivery_decision_hashes CHECK (
                acceptance_criteria_hash ~ '^sha256:[0-9a-f]{64}$'
                AND integration_baseline_hash ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_req_delivery_decision_eligibility CHECK (
                jsonb_typeof(eligibility_snapshot) = 'object'
            ),
            CONSTRAINT ck_req_delivery_decision_validity CHECK (
                (
                    validity = 'CURRENT'
                    AND invalidated_at IS NULL
                    AND invalidation_reason IS NULL
                )
                OR (
                    validity = 'INVALIDATED'
                    AND invalidated_at IS NOT NULL
                    AND length(btrim(invalidation_reason)) > 0
                    AND invalidated_at >= decided_at
                )
            )
        )
        """
    )

    op.execute(
        "ALTER TABLE requirement.requirement "
        "ADD CONSTRAINT fk_req_current_selection FOREIGN KEY "
        "(current_integration_baseline_selection_id, id) "
        "REFERENCES requirement.integration_baseline_selection (id, requirement_id), "
        "ADD CONSTRAINT fk_req_current_acceptance_gate FOREIGN KEY "
        "(current_acceptance_gate_id, id) "
        "REFERENCES requirement.delivery_gate (id, requirement_id)"
    )

    op.execute(
        "GRANT SELECT, INSERT ON "
        "requirement.requirement_delivery_snapshot, "
        "requirement.integration_baseline_selection, "
        "requirement.delivery_gate, "
        "requirement.delivery_gate_assignment, "
        "requirement.delivery_decision TO requirement_rw"
    )
    op.execute(
        "GRANT UPDATE (invalidated_at, invalidation_reason) "
        "ON requirement.integration_baseline_selection TO requirement_rw"
    )
    op.execute(
        "GRANT UPDATE (state, revision, decided_at, invalidated_at, invalidation_reason) "
        "ON requirement.delivery_gate TO requirement_rw"
    )
    op.execute(
        "GRANT UPDATE (superseded_at) ON requirement.delivery_gate_assignment TO requirement_rw"
    )
    op.execute(
        "GRANT UPDATE (validity, invalidated_at, invalidation_reason) "
        "ON requirement.delivery_decision TO requirement_rw"
    )
    op.execute(
        "REVOKE UPDATE (state, requirement_version, required_work_item_set_version, "
        "required_work_item_set_hash, current_sdd_baseline_id, revision, updated_at) "
        "ON requirement.requirement FROM requirement_rw"
    )
    op.execute(
        "GRANT UPDATE (state, requirement_version, required_work_item_set_version, "
        "required_work_item_set_hash, current_sdd_baseline_id, "
        "current_integration_baseline_selection_id, current_acceptance_gate_id, "
        "revision, updated_at) ON requirement.requirement TO requirement_rw"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
            IF EXISTS (SELECT 1 FROM requirement.requirement_delivery_snapshot)
                OR EXISTS (SELECT 1 FROM requirement.integration_baseline_selection)
                OR EXISTS (SELECT 1 FROM requirement.delivery_gate)
                OR EXISTS (SELECT 1 FROM requirement.delivery_gate_assignment)
                OR EXISTS (SELECT 1 FROM requirement.delivery_decision)
                OR EXISTS (
                    SELECT 1 FROM requirement.requirement
                    WHERE state IN ('AWAITING_ACCEPTANCE', 'AWAITING_MERGE', 'COMPLETED')
                )
                OR EXISTS (
                    SELECT 1 FROM requirement.work_item
                    WHERE state IN ('AWAITING_MERGE', 'COMPLETED')
                )
            THEN
                RAISE EXCEPTION 'V0.6 delivery facts prevent Requirement downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "REVOKE UPDATE (state, requirement_version, required_work_item_set_version, "
        "required_work_item_set_hash, current_sdd_baseline_id, "
        "current_integration_baseline_selection_id, current_acceptance_gate_id, "
        "revision, updated_at) ON requirement.requirement FROM requirement_rw"
    )
    op.execute(
        "GRANT UPDATE (state, requirement_version, required_work_item_set_version, "
        "required_work_item_set_hash, current_sdd_baseline_id, revision, updated_at) "
        "ON requirement.requirement TO requirement_rw"
    )
    op.execute(
        "REVOKE ALL ON "
        "requirement.requirement_delivery_snapshot, "
        "requirement.integration_baseline_selection, "
        "requirement.delivery_gate, "
        "requirement.delivery_gate_assignment, "
        "requirement.delivery_decision FROM requirement_rw"
    )
    op.execute(
        "ALTER TABLE requirement.requirement "
        "DROP CONSTRAINT fk_req_current_acceptance_gate, "
        "DROP CONSTRAINT fk_req_current_selection"
    )
    op.execute("DROP TABLE requirement.delivery_decision")
    op.execute("DROP TABLE requirement.delivery_gate_assignment")
    op.execute("DROP TABLE requirement.delivery_gate")
    op.execute("DROP TABLE requirement.integration_baseline_selection")
    op.execute("DROP TABLE requirement.requirement_delivery_snapshot")
    op.execute("ALTER TABLE requirement.work_item DROP CONSTRAINT uq_req_work_item_owner")

    op.execute("ALTER TABLE requirement.requirement DROP CONSTRAINT ck_requirement_state")
    op.execute("ALTER TABLE requirement.work_item DROP CONSTRAINT ck_requirement_work_item_state")
    op.execute(
        """
        ALTER TABLE requirement.requirement
        ADD CONSTRAINT ck_requirement_state CHECK (
            state IN (
                'CREATED', 'PREPARING', 'AWAITING_CONFIRMATION', 'READY',
                'IN_PROGRESS', 'VERIFYING', 'CANCELED'
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE requirement.work_item
        ADD CONSTRAINT ck_requirement_work_item_state CHECK (
            state = 'DRAFT'
            OR (
                state IN ('READY', 'IN_PROGRESS', 'VERIFYING')
                AND assignment_state = 'ASSIGNED'
                AND repository_state = 'BOUND'
            )
            OR state = 'CANCELED'
        )
        """
    )
    op.execute(
        "ALTER TABLE requirement.requirement "
        "DROP CONSTRAINT ck_req_acceptance_criteria_version_hash, "
        "DROP COLUMN current_acceptance_gate_id, "
        "DROP COLUMN current_integration_baseline_selection_id, "
        "DROP COLUMN acceptance_criteria_hash, "
        "DROP COLUMN acceptance_criteria_version"
    )
