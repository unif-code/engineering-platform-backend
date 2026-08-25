"""Requirement baseline: governed delivery facts and reliable effects.

The migration creates only a NOLOGIN privilege role. Infrastructure owns the
runtime login and credential lifecycle.
"""

from alembic import op

revision = "0001_requirement_base"
down_revision = None
branch_labels = ("requirement",)
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS requirement")
    op.execute(
        """
        CREATE TABLE requirement.requirement (
            id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            acceptance_criteria JSONB NOT NULL,
            created_by TEXT NOT NULL,
            initial_repository_id TEXT NOT NULL,
            route_snapshot_version INTEGER NOT NULL,
            route_snapshot_hash TEXT NOT NULL,
            state TEXT NOT NULL,
            record_state TEXT NOT NULL,
            requirement_version INTEGER NOT NULL,
            required_work_item_set_version INTEGER NOT NULL,
            required_work_item_set_hash TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_requirement_type
                CHECK (type IN ('feat', 'fix', 'refactor', 'chore')),
            CONSTRAINT ck_requirement_title CHECK (length(btrim(title)) > 0),
            CONSTRAINT ck_requirement_description CHECK (length(btrim(description)) > 0),
            CONSTRAINT ck_requirement_acceptance
                CHECK (jsonb_typeof(acceptance_criteria) = 'array'),
            CONSTRAINT ck_requirement_created_by CHECK (length(btrim(created_by)) > 0),
            CONSTRAINT ck_requirement_repository
                CHECK (length(btrim(initial_repository_id)) > 0),
            CONSTRAINT ck_requirement_route
                CHECK (route_snapshot_version >= 1 AND length(route_snapshot_hash) > 0),
            CONSTRAINT ck_requirement_state CHECK (
                state IN (
                    'CREATED', 'PREPARING', 'AWAITING_CONFIRMATION', 'READY', 'CANCELED'
                )
            ),
            CONSTRAINT ck_requirement_record_state CHECK (record_state = 'ACTIVE'),
            CONSTRAINT ck_requirement_versions CHECK (
                requirement_version >= 1
                AND required_work_item_set_version >= 1
                AND revision >= 1
                AND length(required_work_item_set_hash) > 0
            ),
            CONSTRAINT ck_requirement_timestamps CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_requirement_workspace_cursor "
        "ON requirement.requirement (workspace_id, created_at, id)"
    )
    op.execute(
        """
        CREATE TABLE requirement.work_item (
            id UUID PRIMARY KEY,
            requirement_id UUID NOT NULL,
            created_by TEXT NOT NULL,
            human_owner_id TEXT,
            executor_type TEXT NOT NULL,
            executor_id TEXT,
            required_capabilities JSONB NOT NULL,
            assignment_state TEXT NOT NULL,
            repository_state TEXT NOT NULL,
            state TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            base_commit_sha TEXT,
            task_branch TEXT,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_requirement_work_item_requirement
                FOREIGN KEY (requirement_id) REFERENCES requirement.requirement(id),
            CONSTRAINT ck_requirement_work_item_created_by
                CHECK (length(btrim(created_by)) > 0),
            CONSTRAINT ck_requirement_work_item_executor_type CHECK (executor_type = 'HUMAN'),
            CONSTRAINT ck_requirement_work_item_capabilities
                CHECK (jsonb_typeof(required_capabilities) = 'array'),
            CONSTRAINT ck_requirement_work_item_assignment CHECK (
                (
                    assignment_state = 'UNASSIGNED'
                    AND human_owner_id IS NULL
                    AND executor_id IS NULL
                )
                OR
                (
                    assignment_state = 'ASSIGNED'
                    AND length(btrim(human_owner_id)) > 0
                    AND executor_id = human_owner_id
                )
            ),
            CONSTRAINT ck_requirement_work_item_repository CHECK (
                length(btrim(repository_id)) > 0
                AND (
                    (
                        repository_state = 'WAITING_REPOSITORY'
                        AND base_commit_sha IS NULL
                        AND task_branch IS NULL
                    )
                    OR
                    (
                        repository_state = 'BOUND'
                        AND length(btrim(base_commit_sha)) > 0
                        AND length(btrim(task_branch)) > 0
                    )
                )
            ),
            CONSTRAINT ck_requirement_work_item_state CHECK (
                (state = 'DRAFT')
                OR
                (
                    state = 'READY'
                    AND assignment_state = 'ASSIGNED'
                    AND repository_state = 'BOUND'
                )
                OR state = 'CANCELED'
            ),
            CONSTRAINT ck_requirement_work_item_revision CHECK (revision >= 1),
            CONSTRAINT ck_requirement_work_item_timestamps CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_requirement_work_item_requirement "
        "ON requirement.work_item (requirement_id, created_at, id)"
    )
    op.execute(
        """
        CREATE TABLE requirement.sdd_baseline (
            id UUID PRIMARY KEY,
            requirement_id UUID NOT NULL,
            requirement_version INTEGER NOT NULL,
            artifact_id TEXT NOT NULL,
            artifact_version TEXT NOT NULL,
            artifact_hash TEXT NOT NULL,
            route_snapshot_version INTEGER NOT NULL,
            route_snapshot_hash TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_requirement_sdd_baseline_requirement
                FOREIGN KEY (requirement_id) REFERENCES requirement.requirement(id),
            CONSTRAINT uq_requirement_sdd_artifact
                UNIQUE (requirement_id, artifact_id, artifact_version, artifact_hash),
            CONSTRAINT uq_requirement_sdd_gate_subject UNIQUE (
                id,
                requirement_id,
                requirement_version,
                artifact_id,
                artifact_version,
                artifact_hash,
                route_snapshot_version,
                route_snapshot_hash
            ),
            CONSTRAINT ck_requirement_sdd_versions
                CHECK (requirement_version >= 1 AND route_snapshot_version >= 1),
            CONSTRAINT ck_requirement_sdd_refs CHECK (
                length(artifact_id) > 0
                AND length(artifact_version) > 0
                AND length(artifact_hash) > 0
                AND length(route_snapshot_hash) > 0
                AND length(created_by) > 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE requirement.gate_instance (
            id UUID PRIMARY KEY,
            gate_type TEXT NOT NULL,
            requirement_id UUID NOT NULL,
            requirement_version INTEGER NOT NULL,
            sdd_baseline_id UUID NOT NULL,
            artifact_id TEXT NOT NULL,
            artifact_version TEXT NOT NULL,
            artifact_hash TEXT NOT NULL,
            route_snapshot_version INTEGER NOT NULL,
            route_snapshot_hash TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            state TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            decided_at TIMESTAMPTZ,
            CONSTRAINT fk_requirement_gate_requirement
                FOREIGN KEY (requirement_id) REFERENCES requirement.requirement(id),
            CONSTRAINT fk_requirement_gate_baseline FOREIGN KEY (
                sdd_baseline_id,
                requirement_id,
                requirement_version,
                artifact_id,
                artifact_version,
                artifact_hash,
                route_snapshot_version,
                route_snapshot_hash
            ) REFERENCES requirement.sdd_baseline (
                id,
                requirement_id,
                requirement_version,
                artifact_id,
                artifact_version,
                artifact_hash,
                route_snapshot_version,
                route_snapshot_hash
            ),
            CONSTRAINT ck_requirement_gate_type
                CHECK (gate_type = 'REQUIREMENT_BASELINE_CONFIRMATION'),
            CONSTRAINT ck_requirement_gate_versions CHECK (
                requirement_version >= 1
                AND route_snapshot_version >= 1
                AND policy_version >= 1
                AND revision >= 1
            ),
            CONSTRAINT ck_requirement_gate_refs CHECK (
                length(artifact_id) > 0
                AND length(artifact_version) > 0
                AND length(artifact_hash) > 0
                AND length(route_snapshot_hash) > 0
            ),
            CONSTRAINT ck_requirement_gate_state CHECK (
                (state = 'OPEN' AND decided_at IS NULL)
                OR (state = 'DECIDED' AND decided_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_requirement_gate_subject "
        "ON requirement.gate_instance (requirement_id, created_at, id)"
    )
    op.execute(
        """
        CREATE TABLE requirement.gate_assignment (
            id UUID PRIMARY KEY,
            gate_instance_id UUID NOT NULL,
            default_reviewer_id TEXT NOT NULL,
            current_reviewer_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            superseded_at TIMESTAMPTZ,
            CONSTRAINT fk_requirement_assignment_gate
                FOREIGN KEY (gate_instance_id) REFERENCES requirement.gate_instance(id),
            CONSTRAINT uq_requirement_assignment_subject UNIQUE (id, gate_instance_id),
            CONSTRAINT uq_requirement_gate_assignment UNIQUE (gate_instance_id, revision),
            CONSTRAINT ck_requirement_assignment_reviewers CHECK (
                length(default_reviewer_id) > 0 AND length(current_reviewer_id) > 0
            ),
            CONSTRAINT ck_requirement_assignment_revision CHECK (revision >= 1),
            CONSTRAINT ck_requirement_assignment_timestamps CHECK (
                superseded_at IS NULL OR superseded_at >= assigned_at
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_requirement_current_gate_assignment "
        "ON requirement.gate_assignment (gate_instance_id) WHERE superseded_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE requirement.decision (
            id UUID PRIMARY KEY,
            gate_instance_id UUID NOT NULL,
            gate_assignment_id UUID NOT NULL,
            reviewer_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL,
            subject_revision INTEGER NOT NULL,
            decided_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT fk_requirement_decision_gate
                FOREIGN KEY (gate_instance_id) REFERENCES requirement.gate_instance(id),
            CONSTRAINT fk_requirement_decision_assignment
                FOREIGN KEY (gate_assignment_id, gate_instance_id)
                REFERENCES requirement.gate_assignment(id, gate_instance_id),
            CONSTRAINT uq_requirement_decision_gate UNIQUE (gate_instance_id),
            CONSTRAINT ck_requirement_decision_reviewer CHECK (length(reviewer_id) > 0),
            CONSTRAINT ck_requirement_decision_outcome
                CHECK (outcome IN ('APPROVED', 'CHANGES_REQUESTED', 'REJECTED')),
            CONSTRAINT ck_requirement_decision_reason CHECK (length(btrim(reason)) > 0),
            CONSTRAINT ck_requirement_decision_revision CHECK (subject_revision >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE requirement.idempotency_record (
            id UUID PRIMARY KEY,
            actor TEXT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            http_status INTEGER,
            result_metadata JSONB,
            sealed_response BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT uq_requirement_idempotency_scope
                UNIQUE (actor, operation, idempotency_key),
            CONSTRAINT ck_requirement_idempotency_values CHECK (
                length(actor) > 0
                AND length(operation) > 0
                AND length(idempotency_key) > 0
                AND length(request_fingerprint) > 0
            ),
            CONSTRAINT ck_requirement_idempotency_state
                CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
            CONSTRAINT ck_requirement_idempotency_result CHECK (
                (
                    state = 'IN_PROGRESS'
                    AND http_status IS NULL
                    AND result_metadata IS NULL
                    AND sealed_response IS NULL
                    AND completed_at IS NULL
                )
                OR
                (
                    state = 'COMPLETED'
                    AND http_status BETWEEN 100 AND 599
                    AND result_metadata IS NOT NULL
                    AND sealed_response IS NOT NULL
                    AND completed_at IS NOT NULL
                )
            ),
            CONSTRAINT ck_requirement_idempotency_timestamps CHECK (
                updated_at >= created_at
                AND (completed_at IS NULL OR completed_at BETWEEN created_at AND updated_at)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE requirement.outbox_message (
            id UUID PRIMARY KEY,
            topic TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id UUID NOT NULL,
            aggregate_version INTEGER NOT NULL,
            payload JSONB NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            published_at TIMESTAMPTZ,
            last_error_code TEXT,
            CONSTRAINT ck_requirement_outbox_values CHECK (
                length(topic) > 0
                AND length(aggregate_type) > 0
                AND aggregate_version >= 1
                AND jsonb_typeof(payload) = 'object'
                AND attempts >= 0
            ),
            CONSTRAINT ck_requirement_outbox_state
                CHECK (state IN ('PENDING', 'PUBLISHED', 'FAILED')),
            CONSTRAINT ck_requirement_outbox_publication CHECK (
                (state = 'PUBLISHED' AND published_at IS NOT NULL)
                OR (state <> 'PUBLISHED' AND published_at IS NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_requirement_outbox_pending "
        "ON requirement.outbox_message (available_at, id) WHERE state IN ('PENDING', 'FAILED')"
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'requirement_rw') THEN
                CREATE ROLE requirement_rw NOLOGIN;
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA requirement TO requirement_rw")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON requirement.requirement, requirement.work_item, "
        "requirement.gate_instance, requirement.gate_assignment, "
        "requirement.idempotency_record, requirement.outbox_message TO requirement_rw"
    )
    op.execute(
        "GRANT SELECT, INSERT ON requirement.sdd_baseline, requirement.decision "
        "TO requirement_rw"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA requirement FROM requirement_rw")
    op.execute("REVOKE USAGE ON SCHEMA requirement FROM requirement_rw")
    op.execute("DROP TABLE requirement.outbox_message")
    op.execute("DROP TABLE requirement.idempotency_record")
    op.execute("DROP TABLE requirement.decision")
    op.execute("DROP TABLE requirement.gate_assignment")
    op.execute("DROP TABLE requirement.gate_instance")
    op.execute("DROP TABLE requirement.sdd_baseline")
    op.execute("DROP TABLE requirement.work_item")
    op.execute("DROP TABLE requirement.requirement")
    op.execute("DROP SCHEMA requirement")
