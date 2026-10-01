"""Pilot questionnaire, verified observations and sales outcomes."""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("leads", sa.Column("verified_issue", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("verified_issue_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("deal_value_cents", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("deal_currency", sa.String(3), server_default="GBP"))
    op.create_table("pilot_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("answers", sa.JSON()),
        sa.Column("source", sa.String(100)), sa.Column("campaign", sa.String(100)), sa.Column("content", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("applicant_name", sa.String(120)), sa.Column("applicant_email", sa.String(254)),
        sa.Column("portfolio", sa.String(500)), sa.Column("ready_this_week", sa.Boolean()),
        sa.Column("applied_at", sa.DateTime(timezone=True)), sa.Column("status", sa.String(30)),
        sa.Column("admin_notes", sa.Text()), sa.Column("notified_at", sa.DateTime(timezone=True)))
    op.create_index("ix_pilot_plans_user_id", "pilot_plans", ["user_id"])
    op.create_table("lead_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.String(36), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True)))
    op.create_index("ix_lead_outcomes_lead_id", "lead_outcomes", ["lead_id"])
    op.create_index("ix_lead_outcomes_user_id", "lead_outcomes", ["user_id"])


def downgrade():
    op.drop_table("lead_outcomes")
    op.drop_table("pilot_plans")
    for col in ("deal_currency", "deal_value_cents", "verified_issue_at", "verified_issue"):
        op.drop_column("leads", col)
