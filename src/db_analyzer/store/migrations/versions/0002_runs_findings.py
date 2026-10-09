"""runs, snapshots, findings, observations; per-connection EXPLAIN gate limits

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("connections") as batch:
        batch.add_column(sa.Column("gate_json", sa.Text(), nullable=False, server_default="{}"))
    op.create_table(
        "runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("connection_id", sa.String(), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=True),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("skipped_json", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
    )
    op.create_index("ix_runs_connection_id", "runs", ["connection_id"])
    op.create_table(
        "snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_snapshots_run_id", "snapshots", ["run_id"])
    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("connection_id", sa.String(), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("first_seen_run", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("last_seen_run", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
        sa.UniqueConstraint("connection_id", "fingerprint"),
    )
    op.create_table(
        "observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("finding_id", sa.Integer(), sa.ForeignKey("findings.id"), nullable=False),
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("ddl", sa.Text(), nullable=True),
        sa.UniqueConstraint("finding_id", "run_id"),
    )
    op.create_index("ix_observations_run_id", "observations", ["run_id"])


def downgrade() -> None:
    op.drop_table("observations")
    op.drop_table("findings")
    op.drop_table("snapshots")
    op.drop_table("runs")
    with op.batch_alter_table("connections") as batch:
        batch.drop_column("gate_json")
