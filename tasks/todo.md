# LeadBlitz v2 — Fix plan (from REVIEW_2026-09-04.md)

## Batch 1 — production-critical (main) ✅ dacbf1e
- [x] Stripe webhook: `.to_dict()` for stripe ≥15, fail closed without secret, insert-first idempotency (unique on checkout session id), persist stripe_customer_id, Founding Member cap
- [x] Passwords: cap at 72 bytes with a form error (bcrypt 5), pin bcrypt
- [x] Server-side email validation (register, profile); escape admin HTML; validate admin amount
- [x] Proxy headers middleware so `request.client.host` is the real client IP
- [x] 401 → `HX-Redirect` for HTMX requests; `/health`; refuse default session secret
- [x] Secure cookie on https; rate limits on login/register/forgot; forgot-password mail in background
- [x] `decrypt()` only swallows InvalidToken
- [x] Single `CREDIT_COSTS`
- [x] Pin deps + requirements.txt lockfile; build.sh uses it
- [x] Migration 007

## Batch 2 — SSRF & abuse (main) ✅ dacbf1e
- [x] `url_safety.safe_get` used by site_fetcher, email_enrichment, pagespeed
- [x] Public score: rate-limit authenticated users too; `/api/pagespeed` rate-limited + cached

## Batch 3 — credit correctness (main) ✅ 70e92be
- [x] Scoring: claim leads, deduct after success / refund, no caching of failures, technographics in cache, re-score, batch error shown
- [x] Search: charge only on new leads, expired token = error, fresh Load-more, scrape timeout safe
- [x] CSV import: charge scoring, pending_credits, streaming size check, header aliases, GROUP BY, resume on startup
- [x] Hunter: charge only on success, user key only, errors shown
- [x] Dashboard SMS count; shared lead filters; bulk selections survive refresh

## Batch 4 — sessions ✅ 9910217
- [x] Session revocation on password change; hashed reset tokens; OAuth state nonce + tz-aware expiry; dead code removed

## Agent A (worktree) — outreach
- [ ] Email send → DB-backed jobs, non-blocking handler, `{% raw %}`, `{{score}}`, templates partial, signature, agency branding in PDF, report error check
- [ ] SMS: commit tracking, E.164, city/score vars
- [ ] Scrape quality, `_nl2br`, SMTP error wrapping, XSS escaping in report/email

## Agent B (worktree) — frontend
- [ ] Global htmx error toast, loading states, cost labels, live credit badge, wide layout + lead rows, utility classes, first-run checklist, a11y, nav

## After merge
- [ ] lead_card / score_detail render `score_error`; public_score_result handles `has_errors`
- [ ] Full test run + local smoke; push `origin` and `render`

## Deploy checklist (Render dashboard — cannot be done from the repo)
- [ ] Set `STRIPE_WEBHOOK_SECRET` (webhooks are now rejected without it; the success page still grants credits meanwhile)
- [ ] Confirm the Stripe webhook endpoint `https://leadblitz.co/api/stripe/webhook` subscribes to `checkout.session.completed` (+ `checkout.session.async_payment_succeeded`)
- [ ] Health check path `/health`
- [ ] Move `leadblitz-db` off the free plan before the 90-day deletion
- [ ] After deploy: every user is logged out once (session token format changed)
- [ ] Watch logs for `Startup recovery:` and `STRIPE_WEBHOOK_SECRET is not set`
