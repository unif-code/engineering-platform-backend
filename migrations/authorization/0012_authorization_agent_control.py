"""Publish the existing Workspace Agent control capability without granting it."""

from alembic import op

revision = "0012_auth_agent_control"
down_revision = "0011_auth_agent_queries"
branch_labels = None
depends_on = None

_ACTIONS = """
    jsonb_build_array(
        jsonb_build_object('capability', 'agent.run.control', 'scopeType', 'WORKSPACE')
    )
"""


def upgrade() -> None:
    op.execute(f"""
        DO $migration$
        DECLARE actual_meta JSONB;
        BEGIN
            SELECT meta INTO actual_meta FROM "authorization".route_registry
            WHERE route_key='agent-runs' AND capability='agent.run.read'
                AND scope_type='WORKSPACE';
            IF actual_meta IS NULL THEN
                RAISE EXCEPTION 'missing managed route: agent-runs';
            END IF;
            IF actual_meta ? 'actionCapabilities'
                AND actual_meta->'actionCapabilities' IS DISTINCT FROM {_ACTIONS}
            THEN
                RAISE EXCEPTION 'conflicting Agent control action: agent-runs';
            END IF;
            UPDATE "authorization".route_registry
            SET meta=jsonb_set(meta, '{{actionCapabilities}}', {_ACTIONS}, true)
            WHERE route_key='agent-runs';
        END
        $migration$
    """)


def downgrade() -> None:
    op.execute(f"""
        UPDATE "authorization".route_registry SET meta=meta - 'actionCapabilities'
        WHERE route_key='agent-runs' AND capability='agent.run.read'
            AND scope_type='WORKSPACE'
            AND meta->'actionCapabilities' IS NOT DISTINCT FROM {_ACTIONS}
    """)
