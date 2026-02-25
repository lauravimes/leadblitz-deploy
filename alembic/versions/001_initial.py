"""Initial schema — 4 tables

Revision ID: 001
Revises:
Create Date: 2026-02-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_type", sa.String(255), nullable=False),
        sa.Column("location", sa.String(255), nullable=False),
        sa.Column("next_page_token", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaigns_user_id", "campaigns", ["user_id"])

    op.create_table(
        "leads",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(500), nullable=False, server_default=""),
        sa.Column("address", sa.Text(), server_default=""),
        sa.Column("phone", sa.String(50), server_default=""),
        sa.Column("website", sa.Text(), server_default=""),
        sa.Column("rating", sa.Float(), server_default="0"),
        sa.Column("review_count", sa.Integer(), server_default="0"),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("heuristic_score", sa.Integer(), nullable=True),
        sa.Column("ai_score", sa.Integer(), nullable=True),
        sa.Column("score_breakdown", sa.JSON(), nullable=True),
        sa.Column("technographics", sa.JSON(), nullable=True),
        sa.Column("stage", sa.String(20), server_default="new"),
        sa.Column("notes", sa.Text(), server_default=""),
        sa.Column("last_scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_leads_user_id", "leads", ["user_id"])
    op.create_index("ix_leads_campaign_id", "leads", ["campaign_id"])
    op.create_index("ix_leads_stage", "leads", ["stage"])

    op.create_table(
        "score_cache",
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("heuristic_result", sa.JSON(), nullable=True),
        sa.Column("ai_result", sa.JSON(), nullable=True),
        sa.Column("final_score", sa.Integer(), server_default="0"),
        sa.Column("confidence", sa.Float(), server_default="0.5"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("url_hash"),
    )


def downgrade() -> None:
    op.drop_table("score_cache")
    op.drop_table("leads")
    op.drop_table("campaigns")
    op.drop_table("users")
