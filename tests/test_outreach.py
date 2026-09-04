"""Outreach tools: email send jobs, signature, merge fields, SMS, client report, scrape quality."""
import os

# Belt and braces: never let the real worker thread race process_due_items() below,
# whichever conftest this module ends up running under.
os.environ.setdefault("SEND_JOBS_WORKER", "0")

from datetime import timedelta  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from app.models import EmailSettings, EmailSignature, Lead, SendJob, SendJobItem, User, UserAPIKeys  # noqa: E402
from app.services import send_jobs
from app.services.client_report import ClientReportError, cached_client_report, generate_client_report, slugify
from app.services.email_enrichment import (
    _filter_emails,
    choose_best_email,
    extract_domain,
    is_blocked_domain,
)
from app.services.email_senders import (
    EmailProviderError,
    _nl2br,
    build_message,
    deliver_email,
    html_to_text,
    prepare_body,
)
from app.services.encryption import encrypt
from app.services.merge_fields import city_from_address, lead_merge_fields, render_merge_fields
from app.services.sms import infer_region, normalize_phone, sms_segments


# --- fixtures -----------------------------------------------------------------------

@pytest.fixture
def account(register):
    """(email, password, user_id) of a freshly registered user; the shared client
    is logged in as them."""
    return register()


@pytest.fixture
def user(db, account):
    return db.get(User, account[2])


@pytest.fixture
def auth(client, account):
    """The shared client, logged in as ``user`` (register() set the cookie)."""
    return client


def _login(client, email, password):
    r = client.post("/auth/login", data={"email": email, "password": password}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "HX-Redirect" in r.headers, r.text


def _lead(db, user, **kw):
    defaults = dict(name="Acme Plumbing", email="info@acme.example", website="https://acme.example",
                    address="12 High St, Reading RG1 2AB, UK", phone="0118 957 1234")
    defaults.update(kw)
    lead = Lead(user_id=user.id, **defaults)
    db.add(lead)
    db.commit()
    return lead


def _smtp_settings(db, user):
    es = EmailSettings(
        user_id=user.id, provider="smtp", smtp_host="smtp.test", smtp_port=587, smtp_username="u",
        smtp_password_encrypted=encrypt("pw"), smtp_from_email="me@agency.test", smtp_use_tls=True,
    )
    db.add(es)
    db.commit()
    return es


# --- body / signature -------------------------------------------------------------

def test_nl2br_only_touches_plain_text():
    assert _nl2br("Hi,\n\nThanks") == "Hi,<br>\n<br>\nThanks"
    assert _nl2br("Hi,\r\nThanks") == "Hi,<br>\nThanks"
    html = "<p>Hi,</p>\n<p>Thanks</p>"
    assert _nl2br(html) == html
    assert _nl2br("line<br>\nline") == "line<br>\nline"
    assert _nl2br("<div>x</div>\n") == "<div>x</div>\n"


def test_signature_is_appended_and_escaped():
    sig = EmailSignature(full_name="Jane <Doe>", position="Founder", company_name="Bright & Co",
                         phone="0118 1", website="bright.example")
    body = prepare_body("Hi,\nthanks", sig)
    assert body.startswith("Hi,<br>\nthanks")
    assert "Jane &lt;Doe&gt;" in body
    assert "Founder · Bright &amp; Co" in body
    assert 'href="https://bright.example"' in body
    # No signature configured -> body untouched
    assert prepare_body("Hi", None) == "Hi"
    assert prepare_body("Hi", EmailSignature()) == "Hi"


def test_build_message_has_text_alternative_and_keeps_attachment_maintype():
    msg = build_message("a@x.test", "b@y.test", "Subj", "<p>Hello <b>there</b></p>",
                        [(b"%PDF-1.4", "report.pdf", "application/pdf"), (b"\x89PNG", "logo.png", "image/png")])
    assert msg.get_content_type() == "multipart/mixed"
    parts = msg.get_payload()
    alt = parts[0]
    assert alt.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in alt.get_payload()] == ["text/plain", "text/html"]
    assert alt.get_payload()[0].get_payload(decode=True).decode() == "Hello there"
    assert parts[1].get_content_type() == "application/pdf"
    assert parts[2].get_content_type() == "image/png"
    assert parts[2].get_filename() == "logo.png"

    plain = build_message("a@x.test", "b@y.test", "S", "Hi<br>there")
    assert plain.get_content_type() == "multipart/alternative"
    assert html_to_text("<p>One</p><p>Two &amp; three</p>") == "One\n\nTwo & three"


def test_deliver_email_wraps_provider_exceptions():
    settings = EmailSettings(provider="smtp", smtp_host="smtp.test", smtp_port=587, smtp_username="u",
                             smtp_password_encrypted=encrypt("pw"), smtp_from_email="me@agency.test", smtp_use_tls=True)
    import smtplib
    with patch("app.services.email_senders.smtplib.SMTP", side_effect=smtplib.SMTPAuthenticationError(535, b"bad")):
        with pytest.raises(EmailProviderError, match="authentication failed"):
            deliver_email(settings, "to@x.test", "S", "B")
    with patch("app.services.email_senders.smtplib.SMTP", side_effect=TimeoutError("t")):
        with pytest.raises(EmailProviderError, match="timed out"):
            deliver_email(settings, "to@x.test", "S", "B")
    with pytest.raises(EmailProviderError, match="No email provider"):
        deliver_email(EmailSettings(provider="none"), "to@x.test", "S", "B")
    with pytest.raises(EmailProviderError, match="No email provider"):
        deliver_email(None, "to@x.test", "S", "B")


# --- merge fields ------------------------------------------------------------------

def test_merge_fields_substitution_including_score_and_city():
    lead = {"name": "Acme", "website": "acme.test", "address": "12 High St, Reading RG4 8US, UK", "score": None}
    fields = lead_merge_fields(lead)
    assert fields["city"] == "Reading"
    assert fields["score"] == ""
    out = render_merge_fields("Hi {{business_name}} ({{ website }}) in {{city}} score {{score}} {{unknown}}", fields)
    assert out == "Hi Acme (acme.test) in Reading score  {{unknown}}"

    lead["score"] = 42
    assert render_merge_fields("{{score}}", lead_merge_fields(lead)) == "42"


def test_city_from_address_variants():
    assert city_from_address("123 Main St, Springfield, IL 62701, USA") == "Springfield"
    assert city_from_address("Reading RG4 8US, UK") == "Reading"
    assert city_from_address("12 High St", "Bristol, UK") == "Bristol"
    assert city_from_address("", None) == "your area"
    assert city_from_address(None, "Leeds") == "Leeds"


# --- SMS -------------------------------------------------------------------------------

def test_phone_normalisation():
    assert normalize_phone("0118 957 1234", "GB") == "+441189571234"
    assert normalize_phone("(212) 555-0123", "US") == "+12125550123"
    assert normalize_phone("+44 118 957 1234", "US") == "+441189571234"  # explicit +CC ignores region
    assert normalize_phone("not a number", "US") is None
    assert normalize_phone("12345", "GB") is None
    assert normalize_phone("", "GB") is None
    assert normalize_phone(None, "GB") is None


def test_infer_region():
    assert infer_region("0118 957 1234", "12 High St, Reading RG1 2AB, UK") == "GB"
    assert infer_region("0118 957 1234", "Somewhere, United Kingdom") == "GB"
    assert infer_region("0118 957 1234", "Springfield, IL 62701, USA") == "US"
    assert infer_region("(212) 555-0123", "Reading, UK") == "US"
    assert infer_region("+44 118 957 1234", "") == "US"  # irrelevant: + numbers ignore region


def test_sms_segments():
    assert sms_segments("") == 0
    assert sms_segments("a" * 160) == 1
    assert sms_segments("a" * 161) == 2
    assert sms_segments("a" * 307) == 3


def test_sms_send_normalises_numbers_persists_tracking_and_reports_errors(auth, db, user):
    good = _lead(db, user, name="Good Ltd", phone="0118 957 1234", address="Reading RG1 2AB, UK", score=None)
    bad = _lead(db, user, name="Bad Ltd", phone="call us!", address="Reading, UK")
    db.add(UserAPIKeys(user_id=user.id, twilio_account_sid="AC123", twilio_auth_token=encrypt("tok"),
                       twilio_phone_number="+15005550006"))
    db.commit()

    sent = []

    def fake_send(to, body, sid, token, from_number):
        sent.append((to, body, sid, token, from_number))
        return {"success": True, "message_sid": "SM1", "status": "queued"}

    with patch("app.routers.sms.send_sms", side_effect=fake_send):
        r = auth.post("/api/sms/send", data={
            "template": "Hi {{business_name}} in {{city}} — score {{score}}",
            "lead_ids": f"{good.id},{bad.id}",
        })
    assert r.status_code == 200
    assert sent == [("+441189571234", "Hi Good Ltd in Reading — score ", "AC123", "tok", "+15005550006")]
    assert "Sent 1 SMS" in r.text
    assert "Bad Ltd" in r.text and "Could not parse" in r.text

    db.expire_all()
    assert db.get(Lead, good.id).sms_sent_count == 1
    assert db.get(Lead, good.id).last_sms_at is not None
    assert db.get(Lead, bad.id).sms_sent_count in (0, None)


def test_sms_send_missing_twilio_keys_is_friendly(auth, db, user):
    lead = _lead(db, user)
    db.add(UserAPIKeys(user_id=user.id, twilio_account_sid="AC123"))
    db.commit()
    r = auth.post("/api/sms/send", data={"template": "Hi", "lead_ids": lead.id})
    assert r.status_code == 200
    assert "Twilio is not fully configured" in r.text
    assert "Auth Token" in r.text and "phone number" in r.text


# --- client report --------------------------------------------------------------------

def test_client_report_failure_raises_and_produces_no_pdf():
    lead_data = {"name": "Acme", "website": "acme.test", "score": 40}
    with patch("app.services.client_report.OpenAI") as fake_openai:
        fake_openai.return_value.chat.completions.create.side_effect = RuntimeError("boom")
        with patch("app.services.pdf_report.generate_client_pdf") as pdf:
            with pytest.raises(ClientReportError, match="boom"):
                generate_client_report(lead_data)
            pdf.assert_not_called()

    # Malformed / empty LLM output is also an error, not an empty report
    with patch("app.services.client_report.OpenAI") as fake_openai:
        msg = MagicMock()
        msg.message.content = '{"executive_summary": "x", "sections": []}'
        fake_openai.return_value.chat.completions.create.return_value.choices = [msg]
        with pytest.raises(ClientReportError, match="no findings"):
            generate_client_report(lead_data)


def test_client_report_cache_invalidated_by_rescoring(db, user):
    lead = _lead(db, user, score=55)
    assert cached_client_report(lead) is None
    from app.services.client_report import store_client_report
    store_client_report(lead, {"sections": [{"title": "t"}], "agency_name": "should-not-be-cached"})
    db.commit()
    assert cached_client_report(lead) == {"sections": [{"title": "t"}]}
    lead.last_scored_at = lead.client_report_at + timedelta(seconds=1)
    assert cached_client_report(lead) is None


def test_report_html_escapes_and_brands_and_pdf_filename_slug(auth, db, user):
    lead = _lead(db, user, name='Bob\'s "Bakery" <script>', score=70)
    db.add(EmailSignature(user_id=user.id, company_name="Bright Agency", website="bright.test", full_name="Jane"))
    db.commit()
    report = {
        "executive_summary": "<img src=x onerror=alert(1)>",
        "overall_grade": "B",
        "sections": [{"title": "<b>SEO</b>", "status": "good", "finding": "f", "impact": "i", "recommendation": "r"}],
        "top_priorities": ["p1"],
    }
    with patch("app.routers.reports.generate_client_report", return_value=dict(report, sections=list(report["sections"]))):
        r = auth.post(f"/api/leads/{lead.id}/report/client/html")
    assert r.status_code == 200
    assert "<iframe" in r.text and "sandbox" in r.text
    # srcdoc is attribute-escaped by Jinja; decode to inspect the inner document
    import html as _h
    inner = _h.unescape(r.text.split('srcdoc="', 1)[1].split('" sandbox', 1)[0])
    assert "<img src=x" not in inner and "&lt;img src=x" in inner
    assert "&lt;b&gt;SEO&lt;/b&gt;" in inner
    assert "Prepared by Bright Agency · Jane · bright.test" in inner

    db.expire_all()
    assert db.get(Lead, lead.id).client_report["sections"][0]["title"] == "<b>SEO</b>"  # cached, unbranded

    # PDF uses the cache (no generation call) and a slugified filename
    with patch("app.routers.reports.generate_client_report") as gen:
        r = auth.post(f"/api/leads/{lead.id}/report/pdf", data={"report_type": "client"})
    gen.assert_not_called()
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'attachment; filename="audit-report-bob-s-bakery-script.pdf"'
    assert r.content.startswith(b"%PDF")
    assert slugify("  Hello World! ") == "hello-world"


def test_report_html_failure_returns_error_not_empty_report(auth, db, user):
    lead = _lead(db, user, score=70)
    with patch("app.routers.reports.generate_client_report", side_effect=ClientReportError("LLM down")):
        r = auth.post(f"/api/leads/{lead.id}/report/client/html")
    assert r.status_code == 200
    assert "LLM down" in r.text and "<iframe" not in r.text


# --- send jobs ------------------------------------------------------------------------

def test_send_job_creation_status_and_worker(auth, db, user, account, register):
    with_email = _lead(db, user, name="With Email", email="a@acme.test", score=None)
    no_email = _lead(db, user, name="No Email", email=None)
    _smtp_settings(db, user)
    db.add(EmailSignature(user_id=user.id, full_name="Jane Agency"))
    db.commit()

    r = auth.post("/api/email/send", data={
        "subject": "Hello {{business_name}}", "body": "Hi {{business_name}},\nfrom {{city}}",
        "lead_ids": f"{with_email.id},{no_email.id}", "send_rate": "0",
    })
    assert r.status_code == 200
    job = db.query(SendJob).filter_by(user_id=user.id).one()
    assert f"/api/email/send/{job.id}/status" in r.text
    assert "0 sent" in r.text and "1 skipped" in r.text and "1 remaining" in r.text
    items = {i.lead_id: i for i in db.query(SendJobItem).filter_by(job_id=job.id).all()}
    assert items[with_email.id].status == "queued" and items[with_email.id].next_send_at is not None
    assert items[no_email.id].status == "skipped"

    # Another user cannot see the job (registering switches the shared client's cookie)
    register()
    assert "not found" in auth.get(f"/api/email/send/{job.id}/status").text
    _login(auth, account[0], account[1])

    delivered = []

    def fake_deliver(settings, to_email, subject, body, attachments=None):
        delivered.append((settings.provider, to_email, subject, body, attachments))
        return {"success": True}

    with patch("app.services.send_jobs.deliver_email", side_effect=fake_deliver):
        assert send_jobs.process_due_items() == 1
        assert send_jobs.process_due_items() == 0  # nothing left

    assert len(delivered) == 1
    provider, to, subject, body, attachments = delivered[0]
    assert (provider, to, subject) == ("smtp", "a@acme.test", "Hello With Email")
    assert body.startswith("Hi With Email,<br>\nfrom Reading")
    assert "Jane Agency" in body  # signature appended
    assert attachments == []

    db.expire_all()
    job = db.get(SendJob, job.id)
    assert (job.status, job.sent, job.failed) == ("completed", 1, 0)
    assert job.completed_at is not None
    lead = db.get(Lead, with_email.id)
    assert lead.emails_sent_count == 1 and lead.last_emailed_at is not None

    r = auth.get(f"/api/email/send/{job.id}/status")
    assert "Complete" in r.text and "1 sent" in r.text and "hx-trigger" not in r.text


def test_send_job_rate_spreads_schedule_and_can_be_cancelled(auth, db, user):
    leads = [_lead(db, user, name=f"L{i}", email=f"l{i}@acme.test") for i in range(3)]
    job = send_jobs.create_send_job(db, user.id, leads, "S", "B", send_rate_per_day=100)
    items = db.query(SendJobItem).filter_by(job_id=job.id).order_by(SendJobItem.next_send_at).all()
    gaps = [(b.next_send_at - a.next_send_at).total_seconds() for a, b in zip(items, items[1:])]
    assert gaps == [864.0, 864.0]

    # Only the first item is due now
    with patch("app.services.send_jobs.deliver_email", return_value={"success": True}) as deliver:
        assert send_jobs.process_due_items() == 1
    assert deliver.call_count == 1

    r = auth.post(f"/api/email/send/{job.id}/cancel")
    assert "Cancelled" in r.text
    db.expire_all()
    statuses = sorted(i.status for i in db.query(SendJobItem).filter_by(job_id=job.id).all())
    assert statuses == ["sent", "skipped", "skipped"]
    assert db.get(SendJob, job.id).status == "cancelled"


def test_send_job_provider_failure_is_recorded_per_lead(db, user):
    lead = _lead(db, user, email="x@acme.test")
    _smtp_settings(db, user)
    job = send_jobs.create_send_job(db, user.id, [lead], "S", "B")
    with patch("app.services.send_jobs.deliver_email", side_effect=EmailProviderError("SMTP authentication failed")):
        send_jobs.process_due_items()
    db.expire_all()
    item = db.query(SendJobItem).filter_by(job_id=job.id).one()
    assert item.status == "failed" and "authentication failed" in item.error
    job = db.get(SendJob, job.id)
    assert (job.status, job.sent, job.failed) == ("completed", 0, 1)
    assert "authentication failed" in job.last_error
    progress = send_jobs.job_progress(db, job)
    assert progress["errors"] == ["Acme Plumbing: SMTP authentication failed"]
    assert db.get(Lead, lead.id).emails_sent_count in (0, None)


def test_send_job_attach_report_failure_does_not_send_and_success_caches(db, user):
    lead = _lead(db, user, email="x@acme.test", score=61)
    _smtp_settings(db, user)
    db.add(EmailSignature(user_id=user.id, company_name="Bright Agency"))
    db.commit()

    job = send_jobs.create_send_job(db, user.id, [lead], "S", "B", attach_report=True)
    with patch("app.services.send_jobs.generate_client_report", side_effect=ClientReportError("LLM down")), \
         patch("app.services.send_jobs.deliver_email") as deliver:
        send_jobs.process_due_items()
    deliver.assert_not_called()
    db.expire_all()
    assert db.query(SendJobItem).filter_by(job_id=job.id).one().status == "failed"
    assert db.get(Lead, lead.id).client_report is None

    report = {"executive_summary": "ok", "overall_grade": "B",
              "sections": [{"title": "SEO", "status": "good", "finding": "f", "impact": "i", "recommendation": "r"}]}
    job2 = send_jobs.create_send_job(db, user.id, [lead], "S", "B", attach_report=True)
    with patch("app.services.send_jobs.generate_client_report", return_value=dict(report)) as gen, \
         patch("app.services.send_jobs.deliver_email", return_value={"success": True}) as deliver:
        send_jobs.process_due_items()
    gen.assert_called_once()
    attachments = deliver.call_args.args[4]
    assert len(attachments) == 1
    pdf_bytes, filename, mime = attachments[0]
    assert (filename, mime) == ("audit-report-acme-plumbing.pdf", "application/pdf")
    assert pdf_bytes.startswith(b"%PDF")
    db.expire_all()
    assert db.get(Lead, lead.id).client_report["sections"][0]["title"] == "SEO"
    assert db.get(SendJob, job2.id).status == "completed"

    # Third send reuses the cache: no LLM call
    job3 = send_jobs.create_send_job(db, user.id, [lead], "S", "B", attach_report=True)
    with patch("app.services.send_jobs.generate_client_report") as gen, \
         patch("app.services.send_jobs.deliver_email", return_value={"success": True}):
        send_jobs.process_due_items()
    gen.assert_not_called()
    db.expire_all()
    assert db.get(SendJob, job3.id).sent == 1


def test_send_job_custom_attachment_is_stored_and_sent(auth, db, user):
    lead = _lead(db, user, email="x@acme.test")
    _smtp_settings(db, user)
    r = auth.post("/api/email/send", data={"subject": "S", "body": "B", "lead_ids": lead.id},
                  files={"attachment": ("brochure.pdf", b"%PDF-1.4 brochure", "application/pdf")})
    assert r.status_code == 200
    job = db.query(SendJob).filter_by(user_id=user.id).one()
    assert job.attachment_name == "brochure.pdf" and bytes(job.attachment_data) == b"%PDF-1.4 brochure"
    with patch("app.services.send_jobs.deliver_email", return_value={"success": True}) as deliver:
        send_jobs.process_due_items()
    assert deliver.call_args.args[4] == [(b"%PDF-1.4 brochure", "brochure.pdf", "application/pdf")]


def test_send_rejects_empty_selection_and_missing_fields(auth):
    r = auth.post("/api/email/send", data={"subject": "S", "body": "B", "lead_ids": ""})
    assert "No leads with email" in r.text
    r = auth.post("/api/email/send", data={"subject": "", "body": "B", "lead_ids": "x"})
    assert "required" in r.text


# --- signature & templates endpoints --------------------------------------------------

def test_signature_post_only_updates_submitted_fields(auth, db, user):
    db.add(EmailSignature(user_id=user.id, full_name="Old", phone="0118 1", website="old.test"))
    db.commit()
    r = auth.post("/api/email/signatures", data={"full_name": "New Name", "company_name": "Co"})
    assert r.status_code == 200 and "Signature saved" in r.text
    db.expire_all()
    sig = db.query(EmailSignature).filter_by(user_id=user.id).one()
    assert (sig.full_name, sig.company_name, sig.phone, sig.website) == ("New Name", "Co", "0118 1", "old.test")

    r = auth.get("/api/email/signature-form")
    assert 'value="New Name"' in r.text and 'value="old.test"' in r.text


def test_templates_endpoint_returns_partial_not_json(auth, db, user):
    r = auth.get("/api/email/templates")
    assert r.headers["content-type"].startswith("text/html")
    assert "No saved templates" in r.text

    r = auth.post("/api/email/templates", data={"name": "Follow-up", "subject": "Hi {{business_name}}", "body": "B"})
    assert "Template saved" in r.text and "Follow-up" in r.text
    assert 'data-subject="Hi {{business_name}}"' in r.text

    r = auth.post("/api/email/templates", data={"name": "", "subject": "x", "body": "y"})
    assert "Give the template a name" in r.text

    from app.models import EmailTemplate
    tpl = db.query(EmailTemplate).filter_by(user_id=user.id).one()
    r = auth.delete(f"/api/email/templates/{tpl.id}")
    assert "Template deleted" in r.text and "No saved templates" in r.text


def test_preview_substitutes_all_fields(auth, db, user):
    lead = _lead(db, user, score=77)
    r = auth.post("/api/email/preview", data={
        "subject": "{{business_name}} — {{score}}", "body": "In {{city}} at {{website}}", "lead_ids": lead.id,
    })
    assert "Acme Plumbing — 77" in r.text
    assert "In Reading at https://acme.example" in r.text


def test_personalize_emits_credits_changed_and_reports_errors(auth, db, user):
    lead = _lead(db, user)
    from app.models import UserCredits
    from app.services.credits import credit_manager
    credit_manager.get_or_create(db, user.id).balance = 5  # registration may already have granted credits
    db.add(EmailSignature(user_id=user.id, base_pitch="We build sites"))
    db.commit()
    with patch("app.services.ai_email.generate_personalized_email", return_value={"subject": "S", "body": "B"}):
        r = auth.post("/api/email/personalize", data={"lead_id": lead.id})
    assert r.status_code == 200 and r.json() == {"subject": "S", "body": "B"}
    assert r.headers.get("HX-Trigger") == "creditsChanged"
    db.expire_all()
    assert db.query(UserCredits).filter_by(user_id=user.id).one().balance == 4

    with patch("app.services.ai_email.generate_personalized_email", side_effect=RuntimeError("quota")):
        r = auth.post("/api/email/personalize", data={"lead_id": lead.id})
    assert r.status_code == 502 and "quota" in r.json()["error"]
    db.expire_all()
    assert db.query(UserCredits).filter_by(user_id=user.id).one().balance == 4  # not charged on failure


# --- scrape quality ---------------------------------------------------------------------

def test_domain_blacklist_matches_exact_or_subdomain_only():
    assert is_blocked_domain("example.com")
    assert is_blocked_domain("mail.example.com")
    assert is_blocked_domain("email.com")
    assert not is_blocked_domain("myemail.com")
    assert not is_blocked_domain("notexample.com")
    assert is_blocked_domain("wix.com") and is_blocked_domain("sentry.io") and is_blocked_domain("facebook.com")

    kept = _filter_emails({
        "hello@myemail.com", "info@sub.example.com", "support@wix.com", "logo@2x.png", "icon@1x.jpeg",
        "font@x.woff2", "data@app.json", "map@bundle.map", "noreply@acme.test", "team@acme.test", "Bad@Acme.TEST",
    })
    assert kept == ["bad@acme.test", "hello@myemail.com", "team@acme.test"]


def test_choose_best_email_prefers_own_domain():
    candidates = ["hello@wix-designer.test", "jane@acme.test", "info@acme.test", "info@other.test"]
    assert choose_best_email(candidates, "https://www.acme.test/contact") == "info@acme.test"
    assert choose_best_email(["jane@acme.test", "info@other.test"], "acme.test") == "jane@acme.test"
    assert choose_best_email(["bob@other.test", "info@other.test"], "acme.test") == "info@other.test"
    assert choose_best_email(["bob@other.test", "zed@other.test"], "acme.test") == "bob@other.test"
    assert choose_best_email(["bob@other.test", "info@other.test"]) == "info@other.test"  # legacy call still works
    assert choose_best_email([]) is None


def test_extract_domain_strips_only_leading_www():
    assert extract_domain("https://www.acme.test/path") == "acme.test"
    assert extract_domain("wwwidgets.test") == "wwwidgets.test"
    assert extract_domain("https://shop.www-tools.test:8443/") == "shop.www-tools.test"
    assert extract_domain("") is None
