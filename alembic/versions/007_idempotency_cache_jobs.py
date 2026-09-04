"""Purchase idempotency, score cache technographics, stored client reports, send jobs

Revision ID: 007
Revises: 006
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).scalar())


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
    ), {"t": table}).scalar())


def upgrade() -> None:
    conn = op.get_bind()

    # One credit grant per Stripe Checkout session (NULLs are allowed to repeat in Postgres)
    op.create_index(
        "uq_credit_transactions_checkout_session",
        "credit_transactions",
        ["stripe_checkout_session_id"],
        unique=True,
        postgresql_where=sa.text("stripe_checkout_session_id IS NOT NULL"),
    )

    if not _column_exists(conn, "score_cache", "technographics"):
        op.add_column("score_cache", sa.Column("technographics", sa.JSON(), nullable=True))

    if not _column_exists(conn, "leads", "client_report"):
        op.add_column("leads", sa.Column("client_report", sa.JSON(), nullable=True))
    if not _column_exists(conn, "leads", "client_report_at"):
        op.add_column("leads", sa.Column("client_report_at", sa.DateTime(timezone=True), nullable=True))

    if not _table_exists(conn, "send_jobs"):
        op.create_table(
            "send_jobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("subject", sa.Text(), nullable=False, server_default=""),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("attach_report", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("attachment_name", sa.String(255), nullable=True),
            sa.Column("attachment_mime", sa.String(255), nullable=True),
            sa.Column("attachment_data", sa.LargeBinary(), nullable=True),
            sa.Column("send_rate_per_day", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sent", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not _table_exists(conn, "send_job_items"):
        op.create_table(
            "send_job_items",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("job_id", sa.String(36), sa.ForeignKey("send_jobs.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("lead_id", sa.String(36), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
            sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True, index=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("send_job_items")
    op.drop_table("send_jobs")
    op.drop_column("leads", "client_report_at")
    op.drop_column("leads", "client_report")
    op.drop_column("score_cache", "technographics")
    op.drop_index("uq_credit_transactions_checkout_session", table_name="credit_transactions")
