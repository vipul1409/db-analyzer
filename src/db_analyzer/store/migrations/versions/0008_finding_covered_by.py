"""findings.covered_by: the analyzer whose scope decides whether a Finding is "fixed?"

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("covered_by", sa.String(), nullable=True))
    # Until now inventory covered these categories, except the size ranking facts (no rule).
    op.execute(
        "UPDATE findings SET covered_by = 'inventory' "
        "WHERE category IN "
        "('size', 'bloat', 'stale_stats', 'unused_index', 'duplicate_index', 'invalid_index') "
        "AND fingerprint <> 'size:' || subject"
    )


def downgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("covered_by")
