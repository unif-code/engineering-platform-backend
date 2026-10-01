"""Register the candidate catalog navigation and finite management action."""

from alembic import op

revision = "0010_auth_model_catalog"
down_revision = "0009_auth_v06_routes"
branch_labels = None
depends_on = None

_VALUES = """
    ('admin.models', 'platform.model.read', 'PLATFORM', 11,
        jsonb_build_object('name', '模型目录', 'order', 11, 'actionCapabilities',
            jsonb_build_array(jsonb_build_object('capability', 'platform.model.manage',
                                               'scopeType', 'PLATFORM'))))
"""


def upgrade() -> None:
    op.execute(
        f"""
        DO $migration$
        DECLARE conflict_key TEXT;
        BEGIN
            SELECT actual.route_key INTO conflict_key
            FROM "authorization".route_registry AS actual
            JOIN (VALUES {_VALUES}) AS desired
                (route_key, capability, scope_type, sort, meta)
              ON desired.route_key = actual.route_key
            WHERE (actual.capability, actual.scope_type, actual.sort, actual.meta)
                IS DISTINCT FROM
                (desired.capability, desired.scope_type, desired.sort, desired.meta)
            LIMIT 1;
            IF conflict_key IS NOT NULL THEN
                RAISE EXCEPTION 'conflicting managed route: %', conflict_key;
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        f"""
        INSERT INTO "authorization".route_registry
            (route_key, capability, scope_type, sort, meta)
        VALUES {_VALUES}
        ON CONFLICT (route_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM "authorization".route_registry AS actual
        USING (VALUES {_VALUES}) AS desired
            (route_key, capability, scope_type, sort, meta)
        WHERE actual.route_key = desired.route_key
          AND (actual.capability, actual.scope_type, actual.sort, actual.meta)
              IS NOT DISTINCT FROM
              (desired.capability, desired.scope_type, desired.sort, desired.meta)
        """
    )
