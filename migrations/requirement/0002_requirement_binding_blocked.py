"""Persist structured Source Control binding blocks without losing the WorkItem."""

from alembic import op

revision = "0002_req_binding_blocked"
down_revision = "0001_requirement_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE requirement.requirement "
        "ADD COLUMN current_sdd_baseline_id UUID"
    )
    op.execute(
        "ALTER TABLE requirement.sdd_baseline "
        "ADD CONSTRAINT uq_requirement_sdd_owner UNIQUE (id, requirement_id)"
    )
    op.execute(
        "ALTER TABLE requirement.requirement "
        "ADD CONSTRAINT fk_requirement_current_sdd_baseline "
        "FOREIGN KEY (current_sdd_baseline_id, id) "
        "REFERENCES requirement.sdd_baseline(id, requirement_id)"
    )
    op.execute(
        "REVOKE UPDATE ON requirement.gate_instance, "
        "requirement.gate_assignment FROM requirement_rw"
    )
    op.execute(
        "GRANT UPDATE (state, revision, decided_at) "
        "ON requirement.gate_instance TO requirement_rw"
    )
    op.execute(
        "GRANT UPDATE (superseded_at) "
        "ON requirement.gate_assignment TO requirement_rw"
    )
    op.execute(
        "ALTER TABLE requirement.work_item "
        "ADD COLUMN repository_blocked_reason_code TEXT, "
        "ADD COLUMN repository_blocked_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE requirement.work_item "
        "DROP CONSTRAINT ck_requirement_work_item_repository"
    )
    op.execute(
        """
        ALTER TABLE requirement.work_item
        ADD CONSTRAINT ck_requirement_work_item_repository CHECK (
            length(btrim(repository_id)) > 0
            AND (
                (
                    repository_state = 'WAITING_REPOSITORY'
                    AND base_commit_sha IS NULL
                    AND task_branch IS NULL
                    AND repository_blocked_reason_code IS NULL
                    AND repository_blocked_at IS NULL
                )
                OR
                (
                    repository_state = 'BLOCKED'
                    AND base_commit_sha IS NULL
                    AND task_branch IS NULL
                    AND repository_blocked_reason_code IN (
                        'CONNECTOR_UNAVAILABLE',
                        'REPOSITORY_NOT_FOUND',
                        'ACCESS_DENIED',
                        'POLICY_DENIED',
                        'BINDING_CONFLICT'
                    )
                    AND repository_blocked_at IS NOT NULL
                )
                OR
                (
                    repository_state = 'BOUND'
                    AND length(btrim(base_commit_sha)) > 0
                    AND length(btrim(task_branch)) > 0
                    AND repository_blocked_reason_code IS NULL
                    AND repository_blocked_at IS NULL
                )
            )
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM requirement.work_item
                WHERE repository_state = 'BLOCKED'
            ) THEN
                RAISE EXCEPTION
                    'blocked repository binding facts prevent Requirement downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "ALTER TABLE requirement.work_item "
        "DROP CONSTRAINT ck_requirement_work_item_repository"
    )
    op.execute(
        "ALTER TABLE requirement.work_item "
        "DROP COLUMN repository_blocked_at, "
        "DROP COLUMN repository_blocked_reason_code"
    )
    op.execute(
        """
        ALTER TABLE requirement.work_item
        ADD CONSTRAINT ck_requirement_work_item_repository CHECK (
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
        )
        """
    )
    op.execute(
        "GRANT UPDATE ON requirement.gate_instance, "
        "requirement.gate_assignment TO requirement_rw"
    )
    op.execute(
        "ALTER TABLE requirement.requirement "
        "DROP CONSTRAINT fk_requirement_current_sdd_baseline"
    )
    op.execute(
        "ALTER TABLE requirement.sdd_baseline "
        "DROP CONSTRAINT uq_requirement_sdd_owner"
    )
    op.execute(
        "ALTER TABLE requirement.requirement "
        "DROP COLUMN current_sdd_baseline_id"
    )
