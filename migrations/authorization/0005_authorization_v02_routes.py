"""activate the completed V0.2 navigation catalog."""

from alembic import op

revision = "0005_authorization_v02_routes"
down_revision = "0004_authorization_pending_set"
branch_labels = None
depends_on = None

_VALUES = """
    ('audit', 'audit.read', 'PLATFORM', 7,
        jsonb_build_object('name', '审计看板', 'order', 7)),
    ('admin.workspaces', 'platform.workspace.manage', 'PLATFORM', 8,
        jsonb_build_object('name', '工作区管理', 'order', 8)),
    ('admin.organization', 'platform.organization.manage', 'PLATFORM', 9,
        jsonb_build_object('name', '组织管理', 'order', 9)),
    ('admin.users', 'identity.account.manage', 'PLATFORM', 13,
        jsonb_build_object('name', '用户管理', 'order', 13)),
    ('admin.grants', 'platform.authorization.manage', 'PLATFORM', 14,
        jsonb_build_object('name', 'Grant 管理', 'order', 14)),
    ('admin.policies', 'platform.configuration.manage', 'PLATFORM', 15,
        jsonb_build_object('name', 'Policy 发布', 'order', 15))
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
