"""Allow provider-neutral opaque Repository identities.

The initial local-only revision used UUID for Repository IDs. Requirement owns
these as opaque identifiers, so this lossless forward migration aligns the
Source Control projection without rewriting any business value.
"""

from alembic import op

revision = "0002_sc_repository_text"
down_revision = "0001_source_control_foundation"
branch_labels = None
depends_on = None

_FOREIGN_KEYS = (
    ("binding_request_inbox", "fk_source_control_inbox_repository"),
    ("source_control_effect", "fk_source_control_effect_repository"),
    ("repository_branch_binding", "fk_source_control_binding_repository"),
    ("webhook_inbox", "fk_source_control_webhook_repository"),
)


def _drop_repository_foreign_keys() -> None:
    for table_name, constraint_name in _FOREIGN_KEYS:
        op.execute(f"ALTER TABLE source_control.{table_name} DROP CONSTRAINT {constraint_name}")


def _add_repository_foreign_keys() -> None:
    for table_name, constraint_name in _FOREIGN_KEYS:
        op.execute(
            f"ALTER TABLE source_control.{table_name} ADD CONSTRAINT {constraint_name} "
            "FOREIGN KEY (repository_id) REFERENCES source_control.workspace_repository(id)"
        )


def upgrade() -> None:
    _drop_repository_foreign_keys()
    op.execute(
        "ALTER TABLE source_control.workspace_repository ALTER COLUMN id TYPE TEXT USING id::text"
    )
    for table_name, _constraint_name in _FOREIGN_KEYS:
        op.execute(
            f"ALTER TABLE source_control.{table_name} "
            "ALTER COLUMN repository_id TYPE TEXT USING repository_id::text"
        )
    _add_repository_foreign_keys()


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM source_control.workspace_repository
                WHERE id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) THEN
                RAISE EXCEPTION
                    'refusing Repository identity downgrade: non-UUID business IDs exist';
            END IF;
        END $$
        """
    )
    _drop_repository_foreign_keys()
    op.execute(
        "ALTER TABLE source_control.workspace_repository ALTER COLUMN id TYPE UUID USING id::uuid"
    )
    for table_name, _constraint_name in _FOREIGN_KEYS:
        op.execute(
            f"ALTER TABLE source_control.{table_name} "
            "ALTER COLUMN repository_id TYPE UUID USING repository_id::uuid"
        )
    _add_repository_foreign_keys()
