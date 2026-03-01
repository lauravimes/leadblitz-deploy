"""Add SMS tracking fields to leads

Revision ID: 005
Revises: 004
Create Date: 2026-03-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("last_sms_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("sms_sent_count", sa.Integer(), server_default="0"))


def downgrade() -> None:
    op.drop_column("leads", "sms_sent_count")
    op.drop_column("leads", "last_sms_at")
