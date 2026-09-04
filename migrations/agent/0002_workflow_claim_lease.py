"""Durable fenced leases for Agent workflow orchestration claims."""

from alembic import op

revision = "0002_workflow_claim_lease"
down_revision = "0001_agent_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent.workflow_command "
        "ADD COLUMN claim_owner TEXT, "
        "ADD COLUMN claim_token UUID, "
        "ADD COLUMN claim_lease_until TIMESTAMPTZ, "
        "ADD COLUMN claim_mode TEXT"
    )
    op.execute(
        "UPDATE agent.workflow_command SET dispatch_attempts=LEAST(dispatch_attempts, 3), "
        "receipt=NULL, dispatched_at=NULL, "
        "last_error_code=CASE WHEN dispatch_attempts >= 3 "
        "THEN 'WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED' "
        "ELSE 'WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN' END, "
        "claim_owner='migration-0002-predecessor-claimed', claim_token=id, "
        "claim_lease_until=TIMESTAMPTZ '1970-01-01 00:00:00+00', claim_mode='RECONCILE' "
        "WHERE state='UNKNOWN' AND receipt->>'outcome'='CLAIMED'"
    )
    op.execute(
        "UPDATE agent.workflow_command SET "
        "state=CASE WHEN dispatch_attempts >= 3 THEN 'UNKNOWN' ELSE 'PLANNED' END, "
        "receipt=NULL, dispatched_at=NULL, "
        "last_error_code=CASE WHEN dispatch_attempts >= 3 "
        "THEN 'WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED' "
        "WHEN last_error_code IS NOT NULL THEN 'WORKFLOW_PRE_CALL_FAILURE' ELSE NULL END "
        "WHERE state='PLANNED'"
    )
    op.execute(
        "UPDATE agent.workflow_command SET state='UNKNOWN', receipt=NULL, dispatched_at=NULL, "
        "last_error_code=CASE WHEN dispatch_attempts >= 3 "
        "THEN 'WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED' "
        "ELSE 'WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN' END "
        "WHERE state='UNKNOWN' AND claim_token IS NULL"
    )
    op.execute(
        "UPDATE agent.workflow_command SET state='UNKNOWN', receipt=NULL, dispatched_at=NULL, "
        "last_error_code=CASE WHEN dispatch_attempts >= 3 "
        "THEN 'WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED' "
        "ELSE 'WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN' END WHERE state='DISPATCHED' AND "
        "(receipt IS NULL OR receipt <> jsonb_build_object('commandKey', command_key, "
        "'outcome', 'ACCEPTED'))"
    )
    op.execute(
        "UPDATE agent.workflow_command SET receipt=NULL, dispatched_at=NULL, "
        "last_error_code=CASE WHEN last_error_code='WORKFLOW_RECONCILIATION_REJECTED' "
        "THEN 'WORKFLOW_RECONCILIATION_REJECTED' ELSE 'DEV_DETERMINISTIC_REJECTION' END "
        "WHERE state='FAILED'"
    )
    op.execute("UPDATE agent.workflow_command SET dispatch_attempts=3 WHERE dispatch_attempts > 3")
    op.execute(
        "ALTER TABLE agent.workflow_command ADD CONSTRAINT ck_agent_command_claim "
        "CHECK ((claim_owner IS NULL AND claim_token IS NULL AND claim_lease_until IS NULL "
        "AND claim_mode IS NULL) OR (length(btrim(claim_owner)) > 0 AND claim_token IS NOT NULL "
        "AND claim_lease_until IS NOT NULL AND claim_mode IN ('DISPATCH', 'RECONCILE')))"
    )
    op.execute(
        "ALTER TABLE agent.workflow_command ADD CONSTRAINT ck_agent_command_claim_attempt_limit "
        "CHECK (dispatch_attempts <= 3)"
    )
    op.execute(
        "ALTER TABLE agent.workflow_command ADD CONSTRAINT ck_agent_command_claim_mode_state "
        "CHECK (claim_mode IS NULL OR (claim_mode='DISPATCH' AND state='PLANNED') OR "
        "(claim_mode='RECONCILE' AND state IN ('PLANNED', 'UNKNOWN')))"
    )
    op.execute(
        "ALTER TABLE agent.workflow_command ADD CONSTRAINT ck_agent_command_outcome_evidence "
        "CHECK ((state='PLANNED' AND receipt IS NULL AND dispatched_at IS NULL AND "
        "(last_error_code IS NULL OR last_error_code='WORKFLOW_PRE_CALL_FAILURE')) OR "
        "(state='DISPATCHED' AND receipt IS NOT NULL AND "
        "receipt=jsonb_build_object('commandKey', command_key, "
        "'outcome', 'ACCEPTED') AND dispatched_at IS NOT NULL AND last_error_code IS NULL) OR "
        "(state='UNKNOWN' AND receipt IS NULL AND dispatched_at IS NULL AND "
        "last_error_code IS NOT NULL AND last_error_code IN "
        "('WORKFLOW_ACKNOWLEDGEMENT_UNKNOWN', 'WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED')) OR "
        "(state='FAILED' AND receipt IS NULL AND dispatched_at IS NULL AND "
        "last_error_code IS NOT NULL AND last_error_code IN "
        "('DEV_DETERMINISTIC_REJECTION', 'WORKFLOW_RECONCILIATION_REJECTED')))"
    )
    op.execute(
        "CREATE INDEX ix_agent_command_claim_lease ON agent.workflow_command "
        "(claim_lease_until, created_at, id) WHERE claim_token IS NOT NULL"
    )
    op.execute(
        "GRANT UPDATE (claim_owner, claim_token, claim_lease_until, claim_mode) "
        "ON agent.workflow_command TO agent_rw"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM agent.workflow_command "
        "WHERE claim_token IS NOT NULL) THEN "
        "RAISE EXCEPTION 'refusing Agent claim-lease downgrade: live claims still exist'; "
        "END IF; END $$"
    )
    op.execute(
        "REVOKE UPDATE (claim_owner, claim_token, claim_lease_until, claim_mode) "
        "ON agent.workflow_command FROM agent_rw"
    )
    op.execute("DROP INDEX agent.ix_agent_command_claim_lease")
    op.execute(
        "ALTER TABLE agent.workflow_command DROP CONSTRAINT ck_agent_command_outcome_evidence"
    )
    op.execute(
        "ALTER TABLE agent.workflow_command DROP CONSTRAINT ck_agent_command_claim_mode_state"
    )
    op.execute(
        "ALTER TABLE agent.workflow_command DROP CONSTRAINT ck_agent_command_claim_attempt_limit"
    )
    op.execute("ALTER TABLE agent.workflow_command DROP CONSTRAINT ck_agent_command_claim")
    op.execute(
        "ALTER TABLE agent.workflow_command DROP COLUMN claim_mode, DROP COLUMN claim_lease_until, "
        "DROP COLUMN claim_token, DROP COLUMN claim_owner"
    )
