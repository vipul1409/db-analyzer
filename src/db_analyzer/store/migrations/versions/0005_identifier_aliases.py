"""connections.alias_identifiers, identifier_aliases

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("connections") as batch:
        batch.add_column(
            sa.Column("alias_identifiers", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_table(
        "identifier_aliases",
        sa.Column("connection_id", sa.String(), sa.ForeignKey("connections.id"), primary_key=True),
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("alias", sa.String(), nullable=False),
        sa.UniqueConstraint("connection_id", "alias"),
    )


def downgrade() -> None:
    op.drop_table("identifier_aliases")
    with op.batch_alter_table("connections") as batch:
        batch.drop_column("alias_identifiers")
