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
    op.add_column("users", sa.Column("signup_ip", sa.String(45), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "signup_ip")
