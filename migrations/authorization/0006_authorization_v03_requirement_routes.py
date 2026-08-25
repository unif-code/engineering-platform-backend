"""Register the V0.3 workspace-scoped Requirement navigation entry."""

from alembic import op

revision = "0006_auth_v03_routes"
down_revision = "0005_authorization_v02_routes"
branch_labels = None
depends_on = None

_VALUES = """
    ('requirements', 'requirement.read', 'WORKSPACE', 20,
        jsonb_build_object('name', 'Requirements', 'order', 20))
"""


def upgrade() -> None:
    op.execute(
        'ALTER TABLE "authorization".route_registry '
        "DROP CONSTRAINT ck_authorization_route_scope"
    )
    op.execute(
        'ALTER TABLE "authorization".route_registry '
        "ADD CONSTRAINT ck_authorization_route_scope "
        "CHECK (scope_type IN ('PLATFORM', 'WORKSPACE'))"
    )
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
    op.execute(
        """
        DO $migration$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM "authorization".route_registry
                WHERE scope_type <> 'PLATFORM'
            ) THEN
                RAISE EXCEPTION 'workspace route extension prevents V0.3 downgrade';
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        'ALTER TABLE "authorization".route_registry '
        "DROP CONSTRAINT ck_authorization_route_scope"
    )
    op.execute(
        'ALTER TABLE "authorization".route_registry '
        "ADD CONSTRAINT ck_authorization_route_scope CHECK (scope_type = 'PLATFORM')"
    )
