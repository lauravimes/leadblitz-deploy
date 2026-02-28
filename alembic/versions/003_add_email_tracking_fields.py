"""Add email tracking fields to leads

Revision ID: 003
Revises: 002
Create Date: 2026-02-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("last_emailed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("emails_sent_count", sa.Integer(), server_default="0"))


def downgrade() -> None:
    op.drop_column("leads", "emails_sent_count")
    op.drop_column("leads", "last_emailed_at")
