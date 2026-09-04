"""Add signup_ip to users for abuse detection

Revision ID: 006
Revises: 005
Create Date: 2026-06-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: prod may already have signup_ip while alembic_version stuck at 005
    # (column applied outside stamp). Plain ADD COLUMN then fails DuplicateColumn.
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'signup_ip'"
        )
    ).scalar()
    if not exists:
        op.add_column("users", sa.Column("signup_ip", sa.String(45), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'signup_ip'"
        )
    ).scalar()
    if exists:
        op.drop_column("users", "signup_ip")
