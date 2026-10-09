"""connections, probes, audit_log

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("adapter_kind", sa.String(), nullable=False),
        sa.Column("dsn_env", sa.String(), nullable=False),
        sa.Column("limits_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "probes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("connection_id", sa.String(), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_probes_connection_id", "probes", ["connection_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("connection_id", sa.String(), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_connection_id", "audit_log", ["connection_id"])
    op.create_index("ix_audit_log_thread_id", "audit_log", ["thread_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("probes")
    op.drop_table("connections")
