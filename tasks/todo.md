# LeadBlitz v2 — Fix plan (from REVIEW_2026-09-04.md)

## Batch 1 — production-critical (main)
- [ ] Stripe webhook: `.to_dict()` for stripe ≥15, fail closed without secret, insert-first idempotency (unique on checkout session id), persist stripe_customer_id, Founding Member cap
- [ ] Passwords: cap at 72 bytes with a form error (bcrypt 5), pin bcrypt
- [ ] Server-side email validation (register, profile); escape admin HTML; validate admin amount
- [ ] Proxy headers middleware so `request.client.host` is the real client IP (fixes free-credit IP dedup + rate limits)
- [ ] 401 → `HX-Redirect` for HTMX requests; `/health`; refuse default session secret
- [ ] Secure cookie on https; rate limits on login/register/forgot; forgot-password mail in background
- [ ] `decrypt()` only swallows InvalidToken
- [ ] Single `CREDIT_COSTS`
- [ ] Pin deps + requirements.txt lockfile; build.sh uses it
- [ ] Migration 007: unique(stripe_checkout_session_id), score_cache.technographics, leads.client_report, send_jobs tables

## Batch 2 — SSRF & abuse (main)
- [ ] `url_safety.safe_get`: scheme/host/IP checks on every redirect hop, size cap, html only — used by site_fetcher, email_enrichment, pagespeed
- [ ] Public score: rate-limit authenticated users too; `/api/pagespeed` rate-limited + cached

## Batch 3 — credit correctness (main)
- [ ] Scoring: claim leads, deduct after success / refund, no caching of failures, store technographics in cache, re-score, render batch error
- [ ] Search: charge only on new leads, INVALID_REQUEST = error, fresh Load-more button, catch scrape timeout
- [ ] CSV import: charge scoring, pending_credits, streaming size check, `removeprefix`, header aliases, GROUP BY status
- [ ] Hunter: charge only on success, no server-key fallback, error shown
- [ ] Dashboard SMS count from leads; admin list one query

## Agent A (worktree) — outreach
- [ ] Email send → DB-backed jobs, non-blocking handler, `{% raw %}`, `{{score}}`, templates partial, signature, agency branding in PDF, report error check
- [ ] SMS: commit tracking, E.164, city/score vars
- [ ] Scrape quality, `_nl2br`, SMTP error wrapping, XSS escaping in report/email

## Agent B (worktree) — frontend
- [ ] Global htmx error toast, loading states, cost labels, live credit badge, wide layout + lead rows, utility classes, first-run checklist, a11y, nav

## Then
- [ ] Merge, run tests + smoke, commit, push origin + render
