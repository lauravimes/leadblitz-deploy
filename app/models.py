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
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    campaigns = relationship("Campaign", back_populates="user", cascade="all, delete-orphan")
    leads = relationship("Lead", back_populates="user", cascade="all, delete-orphan")


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
    campaign_id = Column(String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)

    name = Column(String(500), nullable=False, default="")
    address = Column(Text, default="")
    phone = Column(String(50), default="")
    website = Column(Text, default="")
    rating = Column(Float, default=0.0)
    review_count = Column(Integer, default=0)

    score = Column(Integer, nullable=True)
    heuristic_score = Column(Integer, nullable=True)
    ai_score = Column(Integer, nullable=True)
    score_breakdown = Column(JSON, nullable=True)
    technographics = Column(JSON, nullable=True)

    stage = Column(String(20), default="new", index=True)  # new / reviewing / qualified / rejected
    notes = Column(Text, default="")
    last_scored_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    user = relationship("User", back_populates="leads")
    campaign = relationship("Campaign", back_populates="leads")


class ScoreCache(Base):
    __tablename__ = "score_cache"

    url_hash = Column(String(64), primary_key=True)
    normalized_url = Column(Text, nullable=False)
    heuristic_result = Column(JSON, nullable=True)
    ai_result = Column(JSON, nullable=True)
    final_score = Column(Integer, default=0)
    confidence = Column(Float, default=0.5)
    fetched_at = Column(DateTime(timezone=True), default=_utcnow)
