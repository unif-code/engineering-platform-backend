"""Register the V0.5 Workspace-scoped Requirement delivery capabilities."""

from alembic import op

revision = "0008_auth_v05_routes"
down_revision = "0007_auth_v04_routes"
branch_labels = None
depends_on = None

_V04_ACTION_CAPABILITIES = """
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

_V05_ACTION_CAPABILITIES = """
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
        ),
        jsonb_build_object('capability', 'work_item.execute', 'scopeType', 'WORKSPACE'),
        jsonb_build_object('capability', 'merge_request.merge', 'scopeType', 'WORKSPACE')
    )
"""


def upgrade() -> None:
    op.execute(
        f"""
        DO $migration$
        DECLARE actual_actions JSONB;
        BEGIN
            SELECT meta->'actionCapabilities' INTO actual_actions
            FROM "authorization".route_registry
            WHERE route_key='requirements'
              AND capability='requirement.read'
              AND scope_type='WORKSPACE';

            IF actual_actions IS NULL THEN
                RAISE EXCEPTION 'missing managed V0.4 action capabilities: requirements';
            END IF;
            IF actual_actions IS DISTINCT FROM {_V04_ACTION_CAPABILITIES}
               AND actual_actions IS DISTINCT FROM {_V05_ACTION_CAPABILITIES}
            THEN
                RAISE EXCEPTION 'conflicting V0.5 action capabilities: requirements';
            END IF;

            UPDATE "authorization".route_registry
            SET meta=jsonb_set(
                meta,
                '{{actionCapabilities}}',
                {_V05_ACTION_CAPABILITIES},
                true
            )
            WHERE route_key='requirements';
        END
        $migration$
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $migration$
        DECLARE actual_actions JSONB;
        BEGIN
            SELECT meta->'actionCapabilities' INTO actual_actions
            FROM "authorization".route_registry
            WHERE route_key='requirements';

            IF actual_actions IS DISTINCT FROM {_V05_ACTION_CAPABILITIES}
               AND actual_actions IS DISTINCT FROM {_V04_ACTION_CAPABILITIES}
            THEN
                RAISE EXCEPTION 'conflicting V0.5 action capabilities: requirements';
            END IF;

            UPDATE "authorization".route_registry
            SET meta=jsonb_set(
                meta,
                '{{actionCapabilities}}',
                {_V04_ACTION_CAPABILITIES},
                true
            )
            WHERE route_key='requirements';
        END
        $migration$
        """
    )
