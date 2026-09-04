from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.models import Lead
from app.services.credits import credit_manager
from app.services.url_safety import UnsafeURL, is_safe_url, validate_url
from app.services.lead_filters import apply_lead_filters


# --- URL safety -----------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://10.1.2.3/", "http://192.168.1.1/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
    "http://0.0.0.0/", "file:///etc/passwd", "ftp://example.com/", "http://user:pw@example.com/",
    "http://example.com:22/", "http://100.64.0.1/",
])
def test_unsafe_urls_rejected(url):
    assert not is_safe_url(url)


def test_public_url_accepted():
    assert validate_url("https://example.com/") == "https://example.com/"


def test_safe_get_revalidates_redirects():
    from app.services import url_safety

    class FakeResp:
        def __init__(self, status, headers):
            self.status_code = status
            self.headers = headers
            self.is_redirect = status in (301, 302, 303, 307, 308)
            self.is_permanent_redirect = status in (301, 308)
        def close(self): pass
        def iter_content(self, chunk_size): return iter([b"<html>ok</html>"])

    class FakeSession:
        max_redirects = 0
        def get(self, url, **kw):
            if "example.com" in url:
                return FakeResp(302, {"Location": "http://127.0.0.1/admin"})
            return FakeResp(200, {"Content-Type": "text/html"})
        def close(self): pass

    with patch.object(url_safety.requests, "Session", FakeSession):
        with pytest.raises(UnsafeURL):
            url_safety.safe_get("https://example.com/")


def test_public_score_blocks_internal_url(client):
    r = client.post("/score", data={"url": "http://127.0.0.1:8791/login"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "public website" in r.text


def test_public_score_rate_limit_anonymous(client):
    client.cookies.clear()
    with patch("app.routers.public_score.score_website_hybrid", return_value={"final_score": 50, "has_errors": False,
               "breakdown": {}, "plain_english_report": {}, "technographics": None, "errors": []}):
        for _ in range(5):
            client.post("/score", data={"url": "example.com"}, headers={"HX-Request": "true"})
        r = client.post("/score", data={"url": "example.com"}, headers={"HX-Request": "true"})
    assert "Rate limit" in r.text


# --- Scoring credit correctness ---------------------------------------------------

def _make_lead(uid: int, website="https://example.com", score=None) -> str:
    db = SessionLocal()
    try:
        lead = Lead(user_id=uid, name="Test Co", website=website, score=score)
        db.add(lead)
        db.commit()
        return lead.id
    finally:
        db.close()


FAILED = {"has_errors": True, "final_score": None, "error_message": "We could not load this website.", "errors": ["Timeout"]}
OK = {"has_errors": False, "final_score": 61, "heuristic_score": 30, "ai_score": 31, "breakdown": {"heuristic": {}, "ai": {}},
      "evidence": {}, "ai_justifications": {}, "plain_english_report": {}, "technographics": {"detected": False}, "errors": []}


def test_failed_score_refunds_credit_and_keeps_lead_unscored(logged_in):
    client, uid = logged_in
    lead_id = _make_lead(uid)
    with patch("app.routers.scoring.score_website_hybrid", return_value=FAILED):
        r = client.post(f"/api/score/{lead_id}", headers={"HX-Request": "true", "HX-Target": f"lead-{lead_id}"})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        assert credit_manager.get_balance(db, uid) == 200
        lead = db.get(Lead, lead_id)
        assert lead.score is None
        assert lead.score_breakdown["has_errors"] is True
        refunds = db.execute(text("SELECT count(*) FROM credit_transactions WHERE transaction_type='refund'")).scalar()
        assert refunds == 1
    finally:
        db.close()


def test_successful_score_charges_once_and_rescoring_needs_force(logged_in):
    client, uid = logged_in
    lead_id = _make_lead(uid)
    with patch("app.routers.scoring.score_website_hybrid", return_value=OK) as mocked:
        r1 = client.post(f"/api/score/{lead_id}", headers={"HX-Request": "true"})
        r2 = client.post(f"/api/score/{lead_id}", headers={"HX-Request": "true"})  # no force → no charge
        r3 = client.post(f"/api/score/{lead_id}", data={"force": "1"}, headers={"HX-Request": "true"})
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert mocked.call_count == 2
    assert r1.headers.get("HX-Trigger") == "creditsChanged"
    db = SessionLocal()
    try:
        assert credit_manager.get_balance(db, uid) == 198
        assert db.get(Lead, lead_id).score == 61
    finally:
        db.close()


def test_batch_claims_leads_so_second_batch_finds_nothing(logged_in):
    client, uid = logged_in
    for _ in range(3):
        _make_lead(uid)
    # Neutralise the worker so the claims stay in place for inspection
    with patch("app.routers.scoring._batch_score_worker", lambda *a, **k: None):
        r1 = client.post("/api/score/batch", headers={"HX-Request": "true"})
        r2 = client.post("/api/score/batch", headers={"HX-Request": "true"})
    assert "3 remaining" in r1.text or "remaining" in r1.text
    assert "already being scored" in r2.text or "No unscored" in r2.text
    db = SessionLocal()
    try:
        claimed = db.execute(text("SELECT count(*) FROM leads WHERE import_status='scoring'")).scalar()
        assert claimed == 3
        # Startup recovery releases them
        from app.routers.scoring import reset_stale_claims
        assert reset_stale_claims(db) == 3
    finally:
        db.close()


def test_scorer_does_not_cache_failures(db):
    from app.services import scorer
    with patch("app.services.site_fetcher.fetch_multiple_pages",
               return_value={"combined_html": "", "final_url": "https://x.example", "status": None, "errors": ["Timeout"], "pages": {"homepage": {}}}):
        res = scorer.score_website_hybrid(db, "https://x.example", api_key="k")
    assert res["has_errors"] and res["final_score"] is None
    assert db.execute(text("SELECT count(*) FROM score_cache")).scalar() == 0


def test_lead_filters_search_and_email(register, db):
    _, _, uid = register()
    db.add(Lead(user_id=uid, name="Dentist Smile", email="a@b.com", website=""))
    db.add(Lead(user_id=uid, name="Plumber Joe", email=None, website=""))
    db.commit()
    q = apply_lead_filters(db.query(Lead).filter(Lead.user_id == uid), search="dentist", has_email="1")
    assert [l.name for l in q.all()] == ["Dentist Smile"]
    q2 = apply_lead_filters(db.query(Lead).filter(Lead.user_id == uid), has_email="0")
    assert [l.name for l in q2.all()] == ["Plumber Joe"]


def test_email_all_filtered_respects_search(logged_in):
    client, uid = logged_in
    db = SessionLocal()
    try:
        db.add(Lead(user_id=uid, name="Dentist Smile", email="a@b.com"))
        db.add(Lead(user_id=uid, name="Plumber Joe", email="c@d.com"))
        db.commit()
    finally:
        db.close()
    r = client.post("/api/leads/email-all", data={"q": "dentist"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "bulk_token=" in r.headers.get("HX-Redirect", "")
    token = r.headers["HX-Redirect"].split("bulk_token=")[1]
    from app.routers.leads import get_bulk_selection
    sel = get_bulk_selection(token, uid)
    assert sel and len(sel["lead_ids"]) == 1
    # not single-use: fetching again still works
    assert get_bulk_selection(token, uid) is not None
