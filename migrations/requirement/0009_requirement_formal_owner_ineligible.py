"""Accept the existing Source Control CREATE owner-denial callback reason."""

from alembic import op
from sqlalchemy import text

revision = "0009_req_formal_owner_denial"
down_revision = "0008_req_gate_policy"
branch_labels = None
depends_on = None


def _constraint(*, owner_denial: bool) -> None:
    extra = "'OWNER_INELIGIBLE', " if owner_denial else ""
    op.execute(
        "ALTER TABLE requirement.work_item "
        "DROP CONSTRAINT ck_req_work_item_formal_block, "
        "ADD CONSTRAINT ck_req_work_item_formal_block CHECK ("
        "(formal_delivery_state='BLOCKED' AND formal_blocked_reason_code IN ("
        + extra
        + "'MERGE_ACTOR_INELIGIBLE', 'REPOSITORY_NOT_AUTHORIZED', "
        "'BRANCH_BINDING_MISSING', 'TARGET_BRANCH_NOT_FOUND', "
        "'TARGET_BRANCH_NOT_PROTECTED', 'NO_DELIVERY_COMMIT', 'HEAD_SHA_CHANGED', "
        "'MR_CONFLICT', 'MR_CLOSED', 'MR_CHECKS_BLOCKED', 'MERGE_CONFLICT', "
        "'PROJECT_PROFILE_UNSUPPORTED', 'SOURCE_BRANCH_MISSING_AFTER_INTEGRATION', "
        "'EXTERNAL_MERGE_DRIFT')) OR "
        "(formal_delivery_state<>'BLOCKED' AND formal_blocked_reason_code IS NULL))"
    )


def upgrade() -> None:
    _constraint(owner_denial=True)


def downgrade() -> None:
    # Do not erase or relabel accepted owner facts to fit the older contract.
    op.execute("LOCK TABLE requirement.work_item IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM requirement.work_item "
                "WHERE formal_blocked_reason_code='OWNER_INELIGIBLE')"
            )
        )
        .scalar_one()
    ):
        raise RuntimeError("Cannot downgrade while Formal owner-denial facts exist")
    _constraint(owner_denial=False)
