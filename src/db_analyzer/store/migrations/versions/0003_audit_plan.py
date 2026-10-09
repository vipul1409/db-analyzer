"""audit_log: EXPLAIN cost and rows of gated statements

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.add_column(sa.Column("plan_cost", sa.Float(), nullable=True))
        batch.add_column(sa.Column("plan_rows", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.drop_column("plan_rows")
        batch.drop_column("plan_cost")
