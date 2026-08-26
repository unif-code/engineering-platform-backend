"""Allow Requirement to record Source Control relay outcomes safely."""

from alembic import op

revision = "0003_req_sc_relay"
down_revision = "0002_req_binding_blocked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE requirement.work_item DROP CONSTRAINT ck_requirement_work_item_repository"
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
                        'BINDING_CONFLICT',
                        'OWNER_UNASSIGNED',
                        'OWNER_INELIGIBLE',
                        'REPOSITORY_NOT_AUTHORIZED',
                        'RECONCILIATION_PENDING'
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
                WHERE repository_blocked_reason_code IN (
                    'OWNER_UNASSIGNED',
                    'OWNER_INELIGIBLE',
                    'REPOSITORY_NOT_AUTHORIZED',
                    'RECONCILIATION_PENDING'
                )
            ) THEN
                RAISE EXCEPTION
                    'Source Control binding facts prevent Requirement relay downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "ALTER TABLE requirement.work_item DROP CONSTRAINT ck_requirement_work_item_repository"
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
