import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime, ForeignKey, JSON,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


def _uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False, default="")
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    reset_token = Column(String(255), nullable=True)
    reset_token_expiry = Column(DateTime(timezone=True), nullable=True)
    completed_tutorial = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    campaigns = relationship("Campaign", back_populates="user", cascade="all, delete-orphan")
    leads = relationship("Lead", back_populates="user", cascade="all, delete-orphan")
    credits = relationship("UserCredits", back_populates="user", uselist=False, cascade="all, delete-orphan")
    api_keys = relationship("UserAPIKeys", back_populates="user", uselist=False, cascade="all, delete-orphan")
    email_settings = relationship("EmailSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")
    email_signature = relationship("EmailSignature", back_populates="user", uselist=False, cascade="all, delete-orphan")


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    business_type = Column(String(255), nullable=False)
    location = Column(String(255), nullable=False)
    next_page_token = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    user = relationship("User", back_populates="campaigns")
    leads = relationship("Lead", back_populates="campaign", cascade="all, delete-orphan")


class Lead(Base):
    __tablename__ = "leads"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=True, index=True)

    name = Column(String(500), nullable=False, default="")
    address = Column(Text, default="")
    phone = Column(String(50), default="")
    website = Column(Text, default="")
    rating = Column(Float, default=0.0)
    review_count = Column(Integer, default=0)

    # Email fields
    email = Column(String(255), nullable=True)
    email_source = Column(String(50), nullable=True)  # manual / website / hunter
    email_confidence = Column(Float, nullable=True)
    email_candidates = Column(JSON, nullable=True)

    score = Column(Integer, nullable=True)
    heuristic_score = Column(Integer, nullable=True)
    ai_score = Column(Integer, nullable=True)
    score_breakdown = Column(JSON, nullable=True)
    technographics = Column(JSON, nullable=True)

    stage = Column(String(20), default="new", index=True)  # new / reviewing / qualified / rejected
    notes = Column(Text, default="")
    last_scored_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    # Import tracking
    source = Column(String(50), default="search")  # search / import
    import_id = Column(String(36), ForeignKey("csv_imports.id", ondelete="SET NULL"), nullable=True)
    import_status = Column(String(50), nullable=True)  # queued / scoring / scored / unreachable / pending_credits

    user = relationship("User", back_populates="leads")
    campaign = relationship("Campaign", back_populates="leads")
    csv_import = relationship("CsvImport", back_populates="leads")


class ScoreCache(Base):
    __tablename__ = "score_cache"

    url_hash = Column(String(64), primary_key=True)
    normalized_url = Column(Text, nullable=False)
    heuristic_result = Column(JSON, nullable=True)
    ai_result = Column(JSON, nullable=True)
    final_score = Column(Integer, default=0)
    confidence = Column(Float, default=0.5)
    fetched_at = Column(DateTime(timezone=True), default=_utcnow)


# --- Credits & Payments ---

class UserCredits(Base):
    __tablename__ = "user_credits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    balance = Column(Integer, default=0)
    total_purchased = Column(Integer, default=0)
    total_used = Column(Integer, default=0)
    stripe_customer_id = Column(String(255), nullable=True)

    user = relationship("User", back_populates="credits")


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Integer, nullable=False)
    transaction_type = Column(String(50), nullable=False)  # purchase / usage / subscription_accrual
    description = Column(Text, default="")
    stripe_payment_intent_id = Column(String(255), nullable=True)
    stripe_checkout_session_id = Column(String(255), nullable=True)
    stripe_event_id = Column(String(255), unique=True, nullable=True)
    balance_after = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    stripe_subscription_id = Column(String(255), unique=True, nullable=True)
    package_id = Column(String(100), nullable=False)
    credits_per_period = Column(Integer, default=0)
    status = Column(String(50), default="active")  # active / canceling / canceled / past_due
    current_period_start = Column(DateTime(timezone=True), nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class CreditState(Base):
    __tablename__ = "credit_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    last_issued_at = Column(DateTime(timezone=True), nullable=True)
    issuance_cursor = Column(Float, default=0.0)
    updated_at = Column(DateTime(timezone=True), default=_utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    stripe_session_id = Column(String(255), unique=True, nullable=True)
    amount_cents = Column(Integer, default=0)
    credits_purchased = Column(Integer, default=0)
    plan_name = Column(String(255), default="")
    status = Column(String(50), default="completed")  # completed / pending / failed
    created_at = Column(DateTime(timezone=True), default=_utcnow)


# --- User Config ---

class UserAPIKeys(Base):
    __tablename__ = "user_api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    twilio_account_sid = Column(String(255), nullable=True)
    twilio_auth_token = Column(String(255), nullable=True)
    twilio_phone_number = Column(String(50), nullable=True)
    hunter_api_key = Column(String(255), nullable=True)

    user = relationship("User", back_populates="api_keys")


class EmailSettings(Base):
    __tablename__ = "email_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    provider = Column(String(50), default="none")  # none / gmail / outlook / smtp / sendgrid

    # Gmail OAuth
    gmail_email_address = Column(String(255), nullable=True)
    gmail_access_token = Column(Text, nullable=True)
    gmail_refresh_token = Column(Text, nullable=True)
    gmail_token_expiry = Column(DateTime(timezone=True), nullable=True)

    # Outlook OAuth
    outlook_email_address = Column(String(255), nullable=True)
    outlook_access_token = Column(Text, nullable=True)
    outlook_refresh_token = Column(Text, nullable=True)
    outlook_token_expiry = Column(DateTime(timezone=True), nullable=True)

    # SMTP
    smtp_host = Column(String(255), nullable=True)
    smtp_port = Column(Integer, nullable=True)
    smtp_username = Column(String(255), nullable=True)
    smtp_password_encrypted = Column(Text, nullable=True)
    smtp_from_email = Column(String(255), nullable=True)
    smtp_use_tls = Column(Boolean, default=True)

    # SendGrid
    sendgrid_api_key_encrypted = Column(Text, nullable=True)
    sendgrid_from_email = Column(String(255), nullable=True)

    user = relationship("User", back_populates="email_settings")


class EmailSignature(Base):
    __tablename__ = "email_signatures"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    full_name = Column(String(255), default="")
    position = Column(String(255), default="")
    company_name = Column(String(255), default="")
    phone = Column(String(50), default="")
    website = Column(String(500), default="")
    logo_url = Column(Text, default="")
    disclaimer = Column(Text, default="")
    custom_signature = Column(Text, default="")
    use_custom = Column(Boolean, default=False)
    base_pitch = Column(Text, default="")

    user = relationship("User", back_populates="email_signature")


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    subject = Column(Text, default="")
    body = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class CsvImport(Base):
    __tablename__ = "csv_imports"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(500), default="")
    total_rows = Column(Integer, default=0)
    to_score = Column(Integer, default=0)
    scored_count = Column(Integer, default=0)
    unreachable_count = Column(Integer, default=0)
    pending_count = Column(Integer, default=0)
    pending_credits_count = Column(Integer, default=0)
    skipped_duplicate = Column(Integer, default=0)
    skipped_no_url = Column(Integer, default=0)
    skipped_invalid = Column(Integer, default=0)
    status = Column(String(50), default="in_progress")  # in_progress / completed / partial
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    leads = relationship("Lead", back_populates="csv_import")
