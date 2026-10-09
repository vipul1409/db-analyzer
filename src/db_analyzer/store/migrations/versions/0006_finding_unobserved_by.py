"""findings.unobserved_by: the Run that asks "fixed?"

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("unobserved_by", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("unobserved_by")
