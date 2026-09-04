"""Register explicit V0.6 Workspace actions; never create business Grants."""

from alembic import op

revision = "0009_auth_v06_routes"
down_revision = "0008_auth_v05_routes"
branch_labels = None
depends_on = None

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

_V06_ACTION_CAPABILITIES = """
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
        jsonb_build_object('capability', 'merge_request.merge', 'scopeType', 'WORKSPACE'),
        jsonb_build_object('capability', 'work_item.validation.submit', 'scopeType', 'WORKSPACE'),
        jsonb_build_object('capability', 'requirement.evidence.request', 'scopeType', 'WORKSPACE'),
        jsonb_build_object('capability', 'requirement.evidence.select', 'scopeType', 'WORKSPACE'),
        jsonb_build_object('capability', 'requirement.acceptance.submit', 'scopeType', 'WORKSPACE'),
        jsonb_build_object('capability', 'requirement.acceptance.decide', 'scopeType', 'WORKSPACE'),
        jsonb_build_object('capability', 'formal_merge_request.request', 'scopeType', 'WORKSPACE'),
        jsonb_build_object('capability', 'merge_request.review', 'scopeType', 'WORKSPACE'),
        jsonb_build_object(
            'capability', 'requirement.delivery_gate.assign', 'scopeType', 'WORKSPACE'
        )
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
                RAISE EXCEPTION 'missing managed V0.5 action capabilities: requirements';
            END IF;
            IF actual_actions IS DISTINCT FROM {_V05_ACTION_CAPABILITIES}
               AND actual_actions IS DISTINCT FROM {_V06_ACTION_CAPABILITIES}
            THEN
                RAISE EXCEPTION 'conflicting V0.6 action capabilities: requirements';
            END IF;

            UPDATE "authorization".route_registry
            SET meta=jsonb_set(
                meta,
                '{{actionCapabilities}}',
                {_V06_ACTION_CAPABILITIES},
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

            IF actual_actions IS DISTINCT FROM {_V06_ACTION_CAPABILITIES}
               AND actual_actions IS DISTINCT FROM {_V05_ACTION_CAPABILITIES}
            THEN
                RAISE EXCEPTION 'conflicting V0.6 action capabilities: requirements';
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
