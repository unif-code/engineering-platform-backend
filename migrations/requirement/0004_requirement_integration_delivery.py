"""Record Requirement-owned integration delivery facts."""

from alembic import op

revision = "0004_req_int_delivery"
down_revision = "0003_req_sc_relay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE requirement.work_item "
        "ADD COLUMN integration_delivery_state TEXT NOT NULL DEFAULT 'NOT_STARTED', "
        "ADD COLUMN integration_merge_request_binding_id UUID, "
        "ADD COLUMN integration_blocked_reason_code TEXT, "
        "ADD COLUMN integration_updated_at TIMESTAMPTZ"
    )
    op.execute("ALTER TABLE requirement.requirement DROP CONSTRAINT ck_requirement_state")
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
    op.execute("ALTER TABLE requirement.work_item DROP CONSTRAINT ck_requirement_work_item_state")
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
        """
        ALTER TABLE requirement.work_item
        ADD CONSTRAINT ck_requirement_work_item_integration_delivery CHECK (
            (
                integration_delivery_state IN ('NOT_STARTED', 'IMPLEMENTING', 'MR_PENDING')
                AND integration_merge_request_binding_id IS NULL
            )
            OR (
                integration_delivery_state IN ('MR_OPEN', 'MERGE_PENDING', 'INTEGRATED')
                AND integration_merge_request_binding_id IS NOT NULL
            )
            OR integration_delivery_state IN ('BLOCKED', 'RECONCILIATION_PENDING')
        )
        """
    )
    op.execute(
        """
        ALTER TABLE requirement.work_item
        ADD CONSTRAINT ck_requirement_work_item_integration_blocked_reason CHECK (
            (
                integration_delivery_state = 'BLOCKED'
                AND integration_blocked_reason_code IN (
                    'OWNER_MISMATCH', 'OWNER_INELIGIBLE', 'MERGE_ACTOR_INELIGIBLE',
                    'REPOSITORY_NOT_AUTHORIZED', 'BRANCH_BINDING_MISSING',
                    'TARGET_BRANCH_NOT_FOUND', 'TARGET_BRANCH_NOT_PROTECTED',
                    'NO_DELIVERY_COMMIT', 'HEAD_SHA_CHANGED', 'MR_CONFLICT', 'MR_CLOSED',
                    'MR_CHECKS_BLOCKED', 'MERGE_CONFLICT', 'PROJECT_PROFILE_UNSUPPORTED',
                    'SOURCE_BRANCH_MISSING_AFTER_INTEGRATION', 'EXTERNAL_MERGE_DRIFT',
                    'PROVIDER_UNAVAILABLE', 'RECONCILIATION_PENDING'
                )
            )
            OR (
                integration_delivery_state <> 'BLOCKED'
                AND integration_blocked_reason_code IS NULL
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
                SELECT 1 FROM requirement.requirement
                WHERE state IN ('IN_PROGRESS', 'VERIFYING')
            ) OR EXISTS (
                SELECT 1 FROM requirement.work_item
                WHERE state IN ('IN_PROGRESS', 'VERIFYING')
                    OR integration_delivery_state <> 'NOT_STARTED'
                    OR integration_merge_request_binding_id IS NOT NULL
                    OR integration_blocked_reason_code IS NOT NULL
                    OR integration_updated_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'integration delivery facts prevent Requirement downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        "ALTER TABLE requirement.work_item "
        "DROP CONSTRAINT ck_requirement_work_item_integration_blocked_reason"
    )
    op.execute(
        "ALTER TABLE requirement.work_item "
        "DROP CONSTRAINT ck_requirement_work_item_integration_delivery"
    )
    op.execute("ALTER TABLE requirement.work_item DROP CONSTRAINT ck_requirement_work_item_state")
    op.execute("ALTER TABLE requirement.requirement DROP CONSTRAINT ck_requirement_state")
    op.execute(
        "ALTER TABLE requirement.work_item "
        "DROP COLUMN integration_updated_at, "
        "DROP COLUMN integration_blocked_reason_code, "
        "DROP COLUMN integration_merge_request_binding_id, "
        "DROP COLUMN integration_delivery_state"
    )
    op.execute(
        """
        ALTER TABLE requirement.requirement
        ADD CONSTRAINT ck_requirement_state CHECK (
            state IN ('CREATED', 'PREPARING', 'AWAITING_CONFIRMATION', 'READY', 'CANCELED')
        )
        """
    )
    op.execute(
        """
        ALTER TABLE requirement.work_item
        ADD CONSTRAINT ck_requirement_work_item_state CHECK (
            state = 'DRAFT'
            OR (
                state = 'READY'
                AND assignment_state = 'ASSIGNED'
                AND repository_state = 'BOUND'
            )
            OR state = 'CANCELED'
        )
        """
    )
