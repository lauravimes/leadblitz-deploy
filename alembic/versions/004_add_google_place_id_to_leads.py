"""Add google_place_id to leads

Revision ID: 004
Revises: 003
Create Date: 2026-02-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("google_place_id", sa.String(255), nullable=True))
    op.create_index("ix_leads_google_place_id", "leads", ["google_place_id"])


def downgrade() -> None:
    op.drop_index("ix_leads_google_place_id", table_name="leads")
    op.drop_column("leads", "google_place_id")
