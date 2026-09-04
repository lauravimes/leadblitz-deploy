from sqlalchemy import text

from app.database import SessionLocal


def test_password_change_invalidates_other_sessions(client, register):
    email, password, uid = register()
    # Session A is the registration session; keep its cookie
    cookie_a = client.cookies.get("session")
    assert client.get("/api/credits/balance", headers={"HX-Request": "true"}).status_code == 200

    r = client.post("/api/settings/password", data={"current_password": password, "new_password": "NewHorse22!"})
    assert "Password updated" in r.text

    # Old cookie no longer works
    client.cookies.clear()
    client.cookies.set("session", cookie_a)
    r2 = client.get("/api/credits/balance", headers={"HX-Request": "true"})
    assert r2.status_code == 401

    # New login works with the new password
    client.cookies.clear()
    r3 = client.post("/auth/login", data={"email": email, "password": "NewHorse22!"}, headers={"HX-Request": "true"})
    assert r3.headers.get("HX-Redirect") == "/search"


def test_reset_token_stored_hashed_and_single_use(client, register, monkeypatch):
    email, _, uid = register()
    client.cookies.clear()
    sent = {}
    monkeypatch.setattr("app.routers.auth.send_system_email", lambda to, subj, html: sent.update(html=html) or True)
    r = client.post("/auth/forgot-password", data={"email": email}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    token = sent["html"].split("token=")[1].split('"')[0]
    db = SessionLocal()
    try:
        stored = db.execute(text("SELECT reset_token FROM users WHERE id=:u"), {"u": uid}).scalar()
    finally:
        db.close()
    assert stored and stored != token and len(stored) == 64

    r2 = client.post("/auth/reset-password", data={"token": token, "password": "AnotherHorse3!"}, headers={"HX-Request": "true"})
    assert r2.headers.get("HX-Redirect") == "/login"
    r3 = client.post("/auth/reset-password", data={"token": token, "password": "AnotherHorse4!"}, headers={"HX-Request": "true"})
    assert "Invalid or expired" in r3.text
