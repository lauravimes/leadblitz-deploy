# LeadBlitz v2 — Fix plan (from REVIEW_2026-09-04.md)

All batches below shipped on 4 Sep 2026. Test suite: `tests/` (81 tests).

## Batch 1 — production-critical ✅ dacbf1e
- [x] Stripe webhook: stripe ≥15 compatible, fail closed without secret, insert-first idempotency, stripe_customer_id, Founding Member cap
- [x] Passwords capped at 72 bytes (bcrypt 5); email validation; admin HTML escaped; proxy headers; HX-Redirect on 401; /health; rate limits; secure cookie; single CREDIT_COSTS; lockfile; migration 007

## Batch 2 — SSRF & abuse ✅ dacbf1e
- [x] `url_safety.safe_get` everywhere a user URL is fetched; public scorer limits + PageSpeed cache

## Batch 3 — credit correctness ✅ 70e92be
- [x] Scoring charge-after-success/refund, no cached failures, atomic batch claims, re-score; search charges only on new leads; CSV import charges + pending_credits + resume; Hunter user-key-only; shared lead filters

## Batch 4 — sessions ✅ 9910217
- [x] Session revocation on password change; hashed reset tokens; OAuth nonce; dead code removed

## Outreach (agent-a) ✅ 001e940
- [x] Email sending via DB-backed `send_jobs` worker (survives restarts, cancel button, honest rate labels)
- [x] Client report cached on the lead, agency-branded PDF, failure never emails an empty report, HTML escaped, sandboxed preview
- [x] `{% raw %}` hints, `{{score}}`/`{{city}}` merge fields, templates partial + save form, signature append/pre-fill, text/plain part, provider errors wrapped
- [x] SMS: tracking committed, E.164 via phonenumbers, per-lead errors, segment counter, correct cost label
- [x] Scrape quality: exact-domain blacklist, platform domains, own-domain ranking

## Frontend (agent-b) ✅ 2f7a3cf
- [x] Global error toast, loading states, cost labels, live credit badge, wide layout + lead rows, filters passed to bulk actions, first-run checklist, a11y, sticky nav, copy accuracy

## After merge ✅
- [x] lead_card / score_detail render `score_error`; public_score_result handles `has_errors`
- [x] Full test run + local smoke (register, pages, scoring failure/refund, send job, SMS preview)

## Deploy checklist (Render dashboard — cannot be done from the repo)
- [ ] Set `STRIPE_WEBHOOK_SECRET` (webhooks return 503 without it; the success page still grants credits meanwhile)
- [ ] Confirm the Stripe webhook endpoint `https://leadblitz.co/api/stripe/webhook` subscribes to `checkout.session.completed` (+ `checkout.session.async_payment_succeeded`)
- [ ] Health check path `/health`
- [ ] Move `leadblitz-db` off the free plan before the 90-day deletion
- [ ] After deploy: every user is logged out once (session token format changed)
- [ ] Watch logs for `Startup recovery:` and `STRIPE_WEBHOOK_SECRET is not set`

## Not done / follow-ups
- [ ] Self-host htmx (currently unpkg without an integrity hash)
- [ ] Gmail/Outlook OAuth still has no UI (routes fixed but unreachable); decide finish-or-delete
- [ ] Batch progress / bulk selections still in-process memory (state is in DB; only the progress bar is lost on restart)
- [ ] Old Python 3.9 `.venv` in the repo should be recreated with 3.11+
