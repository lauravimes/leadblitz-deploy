"""Feature expansion — credits, payments, email, CSV, settings

Revision ID: 002
Revises: 001
Create Date: 2026-02-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- User extensions ---
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false")))
    op.add_column("users", sa.Column("reset_token", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("reset_token_expiry", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("completed_tutorial", sa.Boolean(), server_default=sa.text("false")))

    # --- Lead extensions ---
    op.add_column("leads", sa.Column("email", sa.String(255), nullable=True))
    op.add_column("leads", sa.Column("email_source", sa.String(50), nullable=True))
    op.add_column("leads", sa.Column("email_confidence", sa.Float(), nullable=True))
    op.add_column("leads", sa.Column("email_candidates", sa.JSON(), nullable=True))
    op.add_column("leads", sa.Column("source", sa.String(50), server_default="search"))
    op.add_column("leads", sa.Column("import_id", sa.String(36), nullable=True))
    op.add_column("leads", sa.Column("import_status", sa.String(50), nullable=True))

    # Make campaign_id nullable for imported leads
    op.alter_column("leads", "campaign_id", nullable=True)

    # --- CSV Imports (before FK on leads) ---
    op.create_table(
        "csv_imports",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(500), server_default=""),
        sa.Column("total_rows", sa.Integer(), server_default="0"),
        sa.Column("to_score", sa.Integer(), server_default="0"),
        sa.Column("scored_count", sa.Integer(), server_default="0"),
        sa.Column("unreachable_count", sa.Integer(), server_default="0"),
        sa.Column("pending_count", sa.Integer(), server_default="0"),
        sa.Column("pending_credits_count", sa.Integer(), server_default="0"),
        sa.Column("skipped_duplicate", sa.Integer(), server_default="0"),
        sa.Column("skipped_no_url", sa.Integer(), server_default="0"),
        sa.Column("skipped_invalid", sa.Integer(), server_default="0"),
        sa.Column("status", sa.String(50), server_default="in_progress"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_csv_imports_user_id", "csv_imports", ["user_id"])

    # FK from leads.import_id → csv_imports.id
    op.create_foreign_key("fk_leads_import_id", "leads", "csv_imports", ["import_id"], ["id"], ondelete="SET NULL")

    # --- Credits ---
    op.create_table(
        "user_credits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("balance", sa.Integer(), server_default="0"),
        sa.Column("total_purchased", sa.Integer(), server_default="0"),
        sa.Column("total_used", sa.Integer(), server_default="0"),
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )

    op.create_table(
        "credit_transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("transaction_type", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("stripe_payment_intent_id", sa.String(255), nullable=True),
        sa.Column("stripe_checkout_session_id", sa.String(255), nullable=True),
        sa.Column("stripe_event_id", sa.String(255), nullable=True),
        sa.Column("balance_after", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_event_id"),
    )
    op.create_index("ix_credit_transactions_user_id", "credit_transactions", ["user_id"])

    # --- Subscriptions ---
    op.create_table(
        "user_subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(255), nullable=True),
        sa.Column("package_id", sa.String(100), nullable=False),
        sa.Column("credits_per_period", sa.Integer(), server_default="0"),
        sa.Column("status", sa.String(50), server_default="active"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_subscription_id"),
    )
    op.create_index("ix_user_subscriptions_user_id", "user_subscriptions", ["user_id"])

    # --- Credit State ---
    op.create_table(
        "credit_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("last_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issuance_cursor", sa.Float(), server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )

    # --- Payments ---
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stripe_session_id", sa.String(255), nullable=True),
        sa.Column("amount_cents", sa.Integer(), server_default="0"),
        sa.Column("credits_purchased", sa.Integer(), server_default="0"),
        sa.Column("plan_name", sa.String(255), server_default=""),
        sa.Column("status", sa.String(50), server_default="completed"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_session_id"),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"])

    # --- User API Keys ---
    op.create_table(
        "user_api_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("twilio_account_sid", sa.String(255), nullable=True),
        sa.Column("twilio_auth_token", sa.String(255), nullable=True),
        sa.Column("twilio_phone_number", sa.String(50), nullable=True),
        sa.Column("hunter_api_key", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )

    # --- Email Settings ---
    op.create_table(
        "email_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(50), server_default="none"),
        sa.Column("gmail_email_address", sa.String(255), nullable=True),
        sa.Column("gmail_access_token", sa.Text(), nullable=True),
        sa.Column("gmail_refresh_token", sa.Text(), nullable=True),
        sa.Column("gmail_token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outlook_email_address", sa.String(255), nullable=True),
        sa.Column("outlook_access_token", sa.Text(), nullable=True),
        sa.Column("outlook_refresh_token", sa.Text(), nullable=True),
        sa.Column("outlook_token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("smtp_host", sa.String(255), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=True),
        sa.Column("smtp_username", sa.String(255), nullable=True),
        sa.Column("smtp_password_encrypted", sa.Text(), nullable=True),
        sa.Column("smtp_from_email", sa.String(255), nullable=True),
        sa.Column("smtp_use_tls", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("sendgrid_api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("sendgrid_from_email", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )

    # --- Email Signatures ---
    op.create_table(
        "email_signatures",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("full_name", sa.String(255), server_default=""),
        sa.Column("position", sa.String(255), server_default=""),
        sa.Column("company_name", sa.String(255), server_default=""),
        sa.Column("phone", sa.String(50), server_default=""),
        sa.Column("website", sa.String(500), server_default=""),
        sa.Column("logo_url", sa.Text(), server_default=""),
        sa.Column("disclaimer", sa.Text(), server_default=""),
        sa.Column("custom_signature", sa.Text(), server_default=""),
        sa.Column("use_custom", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("base_pitch", sa.Text(), server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )

    # --- Email Templates ---
    op.create_table(
        "email_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("subject", sa.Text(), server_default=""),
        sa.Column("body", sa.Text(), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_templates_user_id", "email_templates", ["user_id"])


def downgrade() -> None:
    op.drop_table("email_templates")
    op.drop_table("email_signatures")
    op.drop_table("email_settings")
    op.drop_table("user_api_keys")
    op.drop_table("payments")
    op.drop_table("credit_states")
    op.drop_table("user_subscriptions")
    op.drop_table("credit_transactions")
    op.drop_table("user_credits")
    op.drop_constraint("fk_leads_import_id", "leads", type_="foreignkey")
    op.drop_table("csv_imports")
    op.drop_column("leads", "import_status")
    op.drop_column("leads", "import_id")
    op.drop_column("leads", "source")
    op.drop_column("leads", "email_candidates")
    op.drop_column("leads", "email_confidence")
    op.drop_column("leads", "email_source")
    op.drop_column("leads", "email")
    op.alter_column("leads", "campaign_id", nullable=False)
    op.drop_column("users", "completed_tutorial")
    op.drop_column("users", "reset_token_expiry")
    op.drop_column("users", "reset_token")
    op.drop_column("users", "is_admin")
