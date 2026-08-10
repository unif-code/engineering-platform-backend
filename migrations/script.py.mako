"""${message}"""
% if imports:

${imports}
% endif

revision = ${repr(up_revision).replace("'", '"')}
down_revision = ${repr(down_revision).replace("'", '"')}
branch_labels = ${repr(branch_labels).replace("'", '"')}
depends_on = ${repr(depends_on).replace("'", '"')}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
