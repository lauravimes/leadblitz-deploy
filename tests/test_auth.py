from sqlalchemy import text

from app.database import SessionLocal
from tests.conftest import unique_email


def _balance(uid: int) -> int:
    db = SessionLocal()
    try:
        return db.execute(text("SELECT balance FROM user_credits WHERE user_id = :u"), {"u": uid}).scalar() or 0
    finally:
        db.close()


def test_register_grants_trial_credits_and_redirects(register):
    email, _, uid = register()
    assert uid is not None
    assert _balance(uid) == 200


def test_register_rejects_invalid_email(client):
    r = client.post("/auth/register", data={"full_name": "X", "email": "not-an-email", "password": "CorrectHorse1!"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "valid email" in r.text
    assert "HX-Redirect" not in r.headers


def test_register_rejects_long_password_with_form_error(client):
    # bcrypt 5 raises on >72 bytes; we must return a form error, not a 500
    r = client.post("/auth/register", data={"full_name": "X", "email": unique_email(), "password": "a" * 100},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "72 characters" in r.text


def test_login_long_password_does_not_500(client, register):
    email, _, _ = register()
    r = client.post("/auth/login", data={"email": email, "password": "b" * 200}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Invalid email or password" in r.text


def test_login_success_sets_cookie(client, register):
    email, password, _ = register()
    client.cookies.clear()
    r = client.post("/auth/login", data={"email": email, "password": password}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/search"
    assert "session" in r.cookies


def test_gmail_alias_does_not_get_second_trial(client, register):
    register("someone@gmail.com")
    client.cookies.clear()
    _, _, uid2 = register("some.one+promo@gmail.com")
    assert _balance(uid2) == 0


def test_same_custom_domain_second_signup_gets_no_trial(client, register):
    register("a@acme-widgets.co.uk")
    client.cookies.clear()
    _, _, uid2 = register("b@acme-widgets.co.uk")
    assert _balance(uid2) == 0


def test_login_rate_limited(client):
    for _ in range(10):
        client.post("/auth/login", data={"email": "nobody@example.com", "password": "x" * 8}, headers={"HX-Request": "true"})
    r = client.post("/auth/login", data={"email": "nobody@example.com", "password": "x" * 8}, headers={"HX-Request": "true"})
    assert r.status_code == 429


def test_unauthenticated_htmx_request_gets_hx_redirect(client):
    client.cookies.clear()
    r = client.get("/api/credits/balance", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers.get("HX-Redirect") == "/login"


def test_unauthenticated_page_request_redirects(client):
    client.cookies.clear()
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_health(client):
    assert client.get("/health").text == "ok"
