import io
from unittest.mock import patch

from sqlalchemy import text

from app.database import SessionLocal
from app.services.credits import credit_manager


PLACES = {
    "places": [
        {"place_id": "p1", "name": "Acme Plumbing", "address": "1 High St, Reading RG1 1AA, UK",
         "phone": "+44 118 123 4567", "website": "https://acme.example", "rating": 4.5, "review_count": 10},
        {"place_id": "p2", "name": "Bravo Heating", "address": "2 High St", "phone": "", "website": "", "rating": 5, "review_count": 3},
    ],
    "next_page_token": "tok1",
}


def test_search_charges_only_when_new_leads(logged_in):
    client, uid = logged_in
    with patch("app.routers.search.search_places", return_value=PLACES), \
         patch("app.routers.search._auto_scrape_emails"):
        r1 = client.post("/api/search", data={"business_type": "plumber", "location": "Reading"}, headers={"HX-Request": "true"})
        assert r1.status_code == 200 and "Acme Plumbing" in r1.text
        assert r1.headers.get("HX-Trigger") == "creditsChanged"
        # Same search again: Google returns the same page → all duplicates → no charge
        r2 = client.post("/api/search", data={"business_type": "plumber", "location": "Reading"}, headers={"HX-Request": "true"})
        assert "No credit was charged" in r2.text or "0 new lead" in r2.text
    db = SessionLocal()
    try:
        assert credit_manager.get_balance(db, uid) == 199
        assert db.execute(text("SELECT count(*) FROM leads WHERE user_id=:u"), {"u": uid}).scalar() == 2
        phone = db.execute(text("SELECT phone FROM leads WHERE name='Acme Plumbing'")).scalar()
        assert phone.startswith("+44")
    finally:
        db.close()


def test_search_more_uses_server_token_and_returns_fresh_button(logged_in):
    client, uid = logged_in
    page2 = {"places": [{"place_id": "p3", "name": "Charlie Drains", "address": "", "phone": "", "website": "", "rating": 0, "review_count": 0}],
             "next_page_token": None}
    with patch("app.routers.search.search_places", side_effect=[PLACES, page2]) as sp, \
         patch("app.routers.search._auto_scrape_emails"):
        r1 = client.post("/api/search", data={"business_type": "plumber", "location": "Reading"}, headers={"HX-Request": "true"})
        campaign_id = r1.text.split('"campaign_id": "')[1].split('"')[0]
        r2 = client.post("/api/search/more", data={"campaign_id": campaign_id, "next_page_token": "stale"}, headers={"HX-Request": "true"})
    assert "Charlie Drains" in r2.text
    assert 'id="load-more"' in r2.text and "No more results" in r2.text
    assert sp.call_args_list[1].kwargs["page_token"] == "tok1"


def test_places_invalid_page_token_is_an_error():
    from app.services import places
    with patch.object(places, "_text_search", return_value={"status": "INVALID_REQUEST"}):
        try:
            places.search_places("key", "plumber", "Reading", page_token="old")
            assert False, "expected PageTokenExpired"
        except places.PageTokenExpired:
            pass


def test_csv_import_charges_per_scored_lead_and_stops_when_broke(logged_in):
    client, uid = logged_in
    db = SessionLocal()
    try:
        credit_manager.deduct_credits(db, uid, "ai_scoring", count=199)  # leave 1 credit
    finally:
        db.close()
    csv_bytes = ("Company,Website,E-mail\n"
                 "One Ltd,https://one.example,a@one.example\n"
                 "Two Ltd,https://two.example,\n"
                 "No Site Ltd,,c@three.example\n").encode()
    OK = {"has_errors": False, "final_score": 40, "heuristic_score": 20, "ai_score": 20, "breakdown": {},
          "evidence": {}, "ai_justifications": {}, "plain_english_report": {}, "technographics": None, "errors": []}
    from app.services import csv_import as csv_service
    with patch("app.routers.scoring.score_website_hybrid", return_value=OK), \
         patch("app.routers.csv.score_import_leads_background") as fake_bg:
        r = client.post("/api/csv/import", files={"file": ("leads.csv", io.BytesIO(csv_bytes), "text/csv")},
                        headers={"HX-Request": "true"})
        assert r.status_code == 200, r.text
        assert "3 leads imported" in r.text and "2 with a website" in r.text
        # run the scoring thread body synchronously
        lead_ids, import_id, user_id = fake_bg.call_args.args
        csv_service._run_scoring_thread(lead_ids, import_id, user_id)
    db = SessionLocal()
    try:
        assert credit_manager.get_balance(db, uid) == 0
        statuses = dict(db.execute(text("SELECT import_status, count(*) FROM leads WHERE user_id=:u GROUP BY import_status"), {"u": uid}).all())
        assert statuses.get("scored") == 1 and statuses.get("pending_credits") == 1
        import_id = db.execute(text("SELECT id FROM csv_imports")).scalar()
    finally:
        db.close()
    s = client.get(f"/api/csv/import/{import_id}/status", headers={"HX-Request": "true"})
    assert "ran out of credits" in s.text


def test_csv_import_rejects_oversize(logged_in):
    client, _ = logged_in
    big = b"business_name\n" + b"x" * (10 * 1024 * 1024 + 1024)
    r = client.post("/api/csv/import", files={"file": ("big.csv", io.BytesIO(big), "text/csv")}, headers={"HX-Request": "true"})
    assert "too large" in r.text


def test_normalize_domain_keeps_leading_w():
    from app.services.csv_import import normalize_domain
    assert normalize_domain("https://www.webfoo.com/") == "webfoo.com"
    assert normalize_domain("wwwhat.co.uk") == "wwwhat.co.uk"


def test_hunter_requires_user_key_and_does_not_charge(logged_in):
    client, uid = logged_in
    from app.models import Lead
    db = SessionLocal()
    try:
        lead = Lead(user_id=uid, name="X", website="https://x.example")
        db.add(lead); db.commit(); lid = lead.id
    finally:
        db.close()
    r = client.post("/api/enrich/hunter", data={"lead_ids": lid}, headers={"HX-Request": "true"})
    assert "Hunter.io API key" in r.text
    db = SessionLocal()
    try:
        assert credit_manager.get_balance(db, uid) == 200
    finally:
        db.close()
