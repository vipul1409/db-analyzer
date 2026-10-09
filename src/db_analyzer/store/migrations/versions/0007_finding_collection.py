"""findings.collection: the collection a Finding's subject is or belongs to

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("collection", sa.String(), nullable=True))
    # Until now every collection-level Finding's subject was its collection.
    op.execute(
        "UPDATE findings SET collection = subject "
        "WHERE category IN ('size', 'bloat', 'stale_stats')"
    )


def downgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("collection")
