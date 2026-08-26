"""Require allowlisted opaque Source Control secret references."""

from alembic import op

revision = "0004_sc_secret_reference"
down_revision = "0003_sc_audit_grant"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_source_control_repository_refs"


def upgrade() -> None:
    op.execute(f"ALTER TABLE source_control.workspace_repository DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"""
        ALTER TABLE source_control.workspace_repository ADD CONSTRAINT {_CONSTRAINT} CHECK (
            length(btrim(project_id)) > 0
            AND length(btrim(project_path)) > 0
            AND length(btrim(connection_ref)) > 0
            AND credential_secret_ref ~
                '^(openbao|secret-ref):[A-Za-z0-9][A-Za-z0-9._/-]{{0,254}}$'
            AND (
                webhook_signing_secret_ref IS NULL
                OR webhook_signing_secret_ref ~
                    '^(openbao|secret-ref):[A-Za-z0-9][A-Za-z0-9._/-]{{0,254}}$'
            )
        )
        """
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE source_control.workspace_repository DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"""
        ALTER TABLE source_control.workspace_repository ADD CONSTRAINT {_CONSTRAINT} CHECK (
            length(btrim(project_id)) > 0
            AND length(btrim(project_path)) > 0
            AND length(btrim(connection_ref)) > 0
            AND length(btrim(credential_secret_ref)) > 0
            AND lower(credential_secret_ref) NOT LIKE '%glpat-%'
            AND (
                webhook_signing_secret_ref IS NULL
                OR (
                    length(btrim(webhook_signing_secret_ref)) > 0
                    AND lower(webhook_signing_secret_ref) NOT LIKE 'whsec\\_%' ESCAPE '\\'
                )
            )
        )
        """
    )
