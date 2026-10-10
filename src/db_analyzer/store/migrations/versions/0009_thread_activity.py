"""threads.last_active_at and threads.preview: listing Threads by activity

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Threads started before this have neither: they list by creation, with no preview.
    with op.batch_alter_table("threads") as batch:
        batch.add_column(sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("preview", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("threads") as batch:
        batch.drop_column("preview")
        batch.drop_column("last_active_at")
