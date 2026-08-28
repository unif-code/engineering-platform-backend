"""Persist V0.4 Route, SDD Artifact, Assignment and Gate policy facts."""

from alembic import op

revision = "0005_req_sdd_human_gate"
down_revision = "0004_req_int_delivery"
branch_labels = None
depends_on = None

_POLICY_CODE = "REQUIREMENT_BASELINE_WORKSPACE_OWNER"
_POLICY_HASH = "sha256:bdfadcc2d2c32fdb9fdf327d45a231cd2e5cb9bf3028f4e09d527fdb50dd8ea2"


def upgrade() -> None:
    op.execute("ALTER TABLE requirement.requirement ADD COLUMN route_snapshot JSONB")
    op.execute(
        """
        UPDATE requirement.requirement
        SET route_snapshot = jsonb_build_object(
            'requirementType', type,
            'requiredCapabilities', jsonb_build_array('code.change'),
            'version', route_snapshot_version
        )
        WHERE route_snapshot IS NULL
        """
    )
    op.execute("ALTER TABLE requirement.requirement ALTER COLUMN route_snapshot SET NOT NULL")
    op.execute(
        """
        ALTER TABLE requirement.requirement
        ADD CONSTRAINT ck_requirement_route_snapshot CHECK (
            jsonb_typeof(route_snapshot) = 'object'
            AND route_snapshot ->> 'requirementType' = type
            AND (route_snapshot ->> 'version')::integer = route_snapshot_version
            AND jsonb_typeof(route_snapshot -> 'requiredCapabilities') = 'array'
            AND (
                NOT route_snapshot ? 'steps'
                OR jsonb_typeof(route_snapshot -> 'steps') = 'array'
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE requirement.sdd_artifact_version (
            artifact_id UUID NOT NULL,
            version INTEGER NOT NULL,
            requirement_id UUID NOT NULL,
            sha256 TEXT NOT NULL,
            state TEXT NOT NULL,
            media_type TEXT NOT NULL,
            trust TEXT NOT NULL,
            content TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_requirement_sdd_artifact_version
                PRIMARY KEY (artifact_id, version),
            CONSTRAINT fk_requirement_sdd_artifact_requirement
                FOREIGN KEY (requirement_id) REFERENCES requirement.requirement(id),
            CONSTRAINT uq_requirement_sdd_artifact_owner
                UNIQUE (requirement_id, artifact_id, version),
            CONSTRAINT ck_requirement_sdd_artifact_version CHECK (version >= 1),
            CONSTRAINT ck_requirement_sdd_artifact_hash CHECK (
                sha256 ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_requirement_sdd_artifact_state CHECK (state = 'AVAILABLE'),
            CONSTRAINT ck_requirement_sdd_artifact_media CHECK (
                media_type = 'text/markdown; charset=utf-8'
            ),
            CONSTRAINT ck_requirement_sdd_artifact_trust CHECK (
                trust = 'TRUSTED_PLAIN_TEXT'
            ),
            CONSTRAINT ck_requirement_sdd_artifact_content CHECK (
                length(content) > 0 AND octet_length(content) <= 200000
            ),
            CONSTRAINT ck_requirement_sdd_artifact_creator CHECK (
                length(btrim(created_by)) > 0
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_requirement_sdd_artifact_owner "
        "ON requirement.sdd_artifact_version (requirement_id, artifact_id, version)"
    )
    op.execute(
        """
        CREATE TABLE requirement.work_item_assignment (
            id UUID PRIMARY KEY,
            work_item_id UUID NOT NULL,
            assignee_id TEXT NOT NULL,
            assigned_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            revision INTEGER NOT NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            superseded_at TIMESTAMPTZ,
            CONSTRAINT fk_requirement_work_item_assignment_item
                FOREIGN KEY (work_item_id) REFERENCES requirement.work_item(id),
            CONSTRAINT uq_requirement_work_item_assignment_revision
                UNIQUE (work_item_id, revision),
            CONSTRAINT ck_requirement_work_item_assignment_people CHECK (
                length(btrim(assignee_id)) > 0
                AND length(btrim(assigned_by)) > 0
            ),
            CONSTRAINT ck_requirement_work_item_assignment_reason CHECK (
                length(btrim(reason)) > 0
            ),
            CONSTRAINT ck_requirement_work_item_assignment_revision CHECK (revision >= 1),
            CONSTRAINT ck_requirement_work_item_assignment_time CHECK (
                superseded_at IS NULL OR superseded_at >= assigned_at
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO requirement.work_item_assignment
            (id, work_item_id, assignee_id, assigned_by, reason, revision, assigned_at)
        SELECT id, id, human_owner_id, created_by, 'V0.3_INITIAL_ASSIGNMENT', 1, created_at
        FROM requirement.work_item
        WHERE assignment_state = 'ASSIGNED'
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_requirement_current_work_item_assignment "
        "ON requirement.work_item_assignment (work_item_id) WHERE superseded_at IS NULL"
    )
    op.execute(
        "ALTER TABLE requirement.gate_instance "
        f"ADD COLUMN policy_code TEXT NOT NULL DEFAULT '{_POLICY_CODE}', "
        f"ADD COLUMN policy_snapshot_hash TEXT NOT NULL DEFAULT '{_POLICY_HASH}'"
    )
    op.execute(
        "ALTER TABLE requirement.gate_instance "
        "ALTER COLUMN policy_code DROP DEFAULT, "
        "ALTER COLUMN policy_snapshot_hash DROP DEFAULT"
    )
    op.execute(
        """
        ALTER TABLE requirement.gate_instance
        ADD CONSTRAINT ck_requirement_gate_policy_snapshot CHECK (
            length(btrim(policy_code)) > 0
            AND policy_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
        )
        """
    )
    op.execute(
        "GRANT SELECT, INSERT ON requirement.sdd_artifact_version, "
        "requirement.work_item_assignment TO requirement_rw"
    )
    op.execute("GRANT UPDATE (superseded_at) ON requirement.work_item_assignment TO requirement_rw")


def downgrade() -> None:
    op.execute(
        "REVOKE UPDATE (superseded_at) ON requirement.work_item_assignment FROM requirement_rw"
    )
    op.execute(
        "REVOKE SELECT, INSERT ON requirement.sdd_artifact_version, "
        "requirement.work_item_assignment FROM requirement_rw"
    )
    op.execute(
        "ALTER TABLE requirement.gate_instance DROP CONSTRAINT ck_requirement_gate_policy_snapshot"
    )
    op.execute(
        "ALTER TABLE requirement.gate_instance "
        "DROP COLUMN policy_snapshot_hash, DROP COLUMN policy_code"
    )
    op.execute("DROP TABLE requirement.work_item_assignment")
    op.execute("DROP TABLE requirement.sdd_artifact_version")
    op.execute("ALTER TABLE requirement.requirement DROP CONSTRAINT ck_requirement_route_snapshot")
    op.execute("ALTER TABLE requirement.requirement DROP COLUMN route_snapshot")
