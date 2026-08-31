"""Register the V0.4 Workspace-scoped Requirement action capabilities."""

from alembic import op

revision = "0007_auth_v04_routes"
down_revision = "0006_auth_v03_routes"
branch_labels = None
depends_on = None

_ACTION_CAPABILITIES = """
    jsonb_build_array(
        jsonb_build_object('capability', 'work_item.create', 'scopeType', 'WORKSPACE'),
        jsonb_build_object('capability', 'work_item.assign', 'scopeType', 'WORKSPACE'),
        jsonb_build_object(
            'capability', 'requirement.baseline.submit', 'scopeType', 'WORKSPACE'
        ),
        jsonb_build_object(
            'capability', 'requirement.baseline.assign', 'scopeType', 'WORKSPACE'
        ),
        jsonb_build_object(
            'capability', 'requirement.baseline.decide', 'scopeType', 'WORKSPACE'
        )
    )
"""


def upgrade() -> None:
    op.execute(
        f"""
        DO $migration$
        DECLARE actual_meta JSONB;
        BEGIN
            SELECT meta INTO actual_meta
            FROM "authorization".route_registry
            WHERE route_key='requirements'
              AND capability='requirement.read'
              AND scope_type='WORKSPACE';

            IF actual_meta IS NULL THEN
                RAISE EXCEPTION 'missing managed route: requirements';
            END IF;
            IF actual_meta ? 'actionCapabilities'
               AND actual_meta->'actionCapabilities' IS DISTINCT FROM {_ACTION_CAPABILITIES}
            THEN
                RAISE EXCEPTION 'conflicting V0.4 action capabilities: requirements';
            END IF;

            UPDATE "authorization".route_registry
            SET meta=jsonb_set(meta, '{{actionCapabilities}}', {_ACTION_CAPABILITIES}, true)
            WHERE route_key='requirements';
        END
        $migration$
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE "authorization".route_registry
        SET meta=meta - 'actionCapabilities'
        WHERE route_key='requirements'
          AND meta->'actionCapabilities' IS NOT DISTINCT FROM {_ACTION_CAPABILITIES}
        """
    )
