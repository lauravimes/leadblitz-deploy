"""Shared test fixtures.

Tests run against a real Postgres database given by DATABASE_URL (default
``postgresql://localhost:5432/leadblitz_test``). Run migrations first:

    DATABASE_URL=postgresql://localhost:5432/leadblitz_test alembic upgrade head
    DATABASE_URL=postgresql://localhost:5432/leadblitz_test python -m pytest -q

Each test gets a clean set of user-scoped tables (truncated, not dropped).
"""

import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/leadblitz_test")
os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_testsecret")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-maps-key")
# Tests drive the email send worker synchronously via send_jobs.process_due_items().
os.environ.setdefault("SEND_JOBS_WORKER", "0")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import create_app
from app.config import get_settings
from app.database import SessionLocal
from app.services import rate_limit


@pytest.fixture(scope="session")
def app():
    get_settings.cache_clear()
    return create_app()


@pytest.fixture(scope="session")
def client(app):
    return TestClient(app)


TRUNCATE_TABLES = [
    "send_job_items", "send_jobs", "credit_transactions", "payments", "user_credits",
    "leads", "campaigns", "csv_imports", "email_templates", "email_signatures",
    "email_settings", "user_api_keys", "user_subscriptions", "credit_states", "users",
    "score_cache",
]


@pytest.fixture(autouse=True)
def clean_db():
    db = SessionLocal()
    try:
        db.execute(text("TRUNCATE " + ", ".join(TRUNCATE_TABLES) + " RESTART IDENTITY CASCADE"))
        db.commit()
    finally:
        db.close()
    for name in dir(rate_limit):
        obj = getattr(rate_limit, name)
        if isinstance(obj, rate_limit.RateLimiter):
            obj.reset()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def register(client):
    """Register a user through the real endpoint; returns (email, password, user_id)."""
    def _register(email: str | None = None, password: str = "CorrectHorse1!", name: str = "Test User"):
        email = email or unique_email()
        r = client.post(
            "/auth/register",
            data={"full_name": name, "email": email, "password": password},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200, r.text
        assert "HX-Redirect" in r.headers, r.text
        db = SessionLocal()
        try:
            uid = db.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email}).scalar()
        finally:
            db.close()
        return email, password, uid
    return _register


@pytest.fixture
def logged_in(client, register):
    """A client with a valid session cookie. Returns (client, user_id)."""
    email, password, uid = register()
    # register() already set the cookie on the shared client
    return client, uid
