import json
import time

import stripe
from sqlalchemy import text

from app.database import SessionLocal
from app.services.credits import credit_manager, CREDIT_COSTS
from app.services.stripe_client import CREDIT_PACKAGES


def _signed(payload: dict, secret: str = "whsec_testsecret") -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    ts = int(time.time())
    sig = stripe.WebhookSignature._compute_signature(f"{ts}.{body.decode()}", secret)
    return body, f"t={ts},v1={sig}"


def _event(uid: int, session_id: str = "cs_test_1", credits: int = 100, event_id: str = "evt_1") -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": session_id,
            "object": "checkout.session",
            "payment_status": "paid",
            "customer": "cus_123",
            "metadata": {"user_id": str(uid), "credits": str(credits), "plan_name": "Starter",
                         "amount_cents": "1500", "package_id": "starter"},
        }},
    }


def test_costs_single_source_of_truth():
    # The credits page must render the same table that deduct_credits charges from
    from app.services import stripe_client
    assert not hasattr(stripe_client, "CREDIT_COSTS")
    assert CREDIT_COSTS["lead_search"] == 1 and CREDIT_COSTS["sms_send"] == 0


def test_deduct_and_refund(register, db):
    _, _, uid = register()
    ok, bal = credit_manager.deduct_credits(db, uid, "ai_scoring", description="t")
    assert ok and bal == 199
    new_bal = credit_manager.refund_credits(db, uid, "ai_scoring", description="r")
    assert new_bal == 200
    used = db.execute(text("SELECT total_used FROM user_credits WHERE user_id=:u"), {"u": uid}).scalar()
    assert used == 0


def test_deduct_insufficient(register, db):
    _, _, uid = register()
    ok, bal = credit_manager.deduct_credits(db, uid, "hunter_enrichment", count=200)
    assert not ok and bal == 200


def test_record_purchase_is_idempotent(register, db):
    _, _, uid = register()
    assert credit_manager.record_purchase(db, uid, 100, "Starter", 1500, "cs_abc", stripe_event_id="evt_a") is True
    assert credit_manager.record_purchase(db, uid, 100, "Starter", 1500, "cs_abc", stripe_event_id="evt_b") is False
    assert credit_manager.get_balance(db, uid) == 300
    payments = db.execute(text("SELECT count(*) FROM payments WHERE user_id=:u"), {"u": uid}).scalar()
    assert payments == 1


def test_webhook_rejects_unsigned(client, register):
    _, _, uid = register()
    r = client.post("/api/stripe/webhook", content=json.dumps(_event(uid)), headers={"content-type": "application/json"})
    assert r.status_code == 400
    db = SessionLocal()
    try:
        assert credit_manager.get_balance(db, uid) == 200
    finally:
        db.close()


def test_webhook_signed_grants_once_and_replay_is_noop(client, register):
    _, _, uid = register()
    body, sig = _signed(_event(uid))
    r = client.post("/api/stripe/webhook", content=body, headers={"stripe-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200, r.text
    # replay same session under a new event id
    body2, sig2 = _signed(_event(uid, event_id="evt_2"))
    r2 = client.post("/api/stripe/webhook", content=body2, headers={"stripe-signature": sig2, "content-type": "application/json"})
    assert r2.status_code == 200
    db = SessionLocal()
    try:
        assert credit_manager.get_balance(db, uid) == 300
        cust = db.execute(text("SELECT stripe_customer_id FROM user_credits WHERE user_id=:u"), {"u": uid}).scalar()
        assert cust == "cus_123"
    finally:
        db.close()


def test_webhook_ignores_unpaid_session(client, register):
    _, _, uid = register()
    ev = _event(uid, session_id="cs_unpaid")
    ev["data"]["object"]["payment_status"] = "unpaid"
    body, sig = _signed(ev)
    r = client.post("/api/stripe/webhook", content=body, headers={"stripe-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        assert credit_manager.get_balance(db, uid) == 200
    finally:
        db.close()


def test_checkout_rejects_unknown_package(logged_in):
    client, _ = logged_in
    r = client.post("/api/credits/checkout", data={"package_id": "nope"})
    assert r.status_code == 400


def test_founding_member_cap(register, db):
    from app.routers.credits import package_sold_out
    _, _, uid = register()
    cap = CREDIT_PACKAGES["founding_member"]["max_buyers"]
    for i in range(cap):
        credit_manager.record_purchase(db, uid, 2000, "Founding Member", 9900, f"cs_fm_{i}")
    assert package_sold_out(db, "founding_member") is True
    assert package_sold_out(db, "starter") is False


def test_admin_html_is_escaped(client, register, db):
    _, _, admin_id = register("admin@example.com")
    db.execute(text("UPDATE users SET is_admin = true WHERE id = :u"), {"u": admin_id})
    db.commit()
    client.cookies.clear()
    evil = 'x"><img src=x onerror=alert(1)>@evil.com'
    # cannot register that email (validation) — insert directly to simulate legacy data
    db.execute(text("INSERT INTO users (email, password_hash, full_name, is_active, is_admin, created_at) "
                    "VALUES (:e, 'h', 'Evil', true, false, now())"), {"e": evil})
    db.commit()
    target_id = db.execute(text("SELECT id FROM users WHERE email = :e"), {"e": evil}).scalar()
    # log in as admin
    client.post("/auth/login", data={"email": "admin@example.com", "password": "CorrectHorse1!"}, headers={"HX-Request": "true"})
    r = client.post("/api/admin/credits/add", data={"user_id": target_id, "amount": 5})
    assert r.status_code == 200
    assert "<img" not in r.text and "&lt;img" in r.text
    r2 = client.post("/api/admin/credits/add", data={"user_id": target_id, "amount": -5})
    assert "between 1 and" in r2.text
