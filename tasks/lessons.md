# Lessons (LeadBlitz v2)

- **Unpinned dependencies broke prod twice in one deploy** (stripe 15 changed StripeObject away from dict; bcrypt 5 raises on >72-byte passwords). `requirements.txt` is now the lockfile — regenerate with `uv pip compile pyproject.toml -o requirements.txt --python-version 3.11` and commit it; never let Render `pip install -e .` resolve fresh.
- **Two remotes**: `origin` (lauravimes/leadblitz-v2) is the product repo; `render` (lauravimes/leadblitz-deploy) is what Render builds. Pushing only `origin` changes nothing in prod.
- **Migrations must be idempotent** for columns that may already exist in prod (006 pattern with information_schema checks); prod's `alembic_version` had lagged the schema.
- **Behind Render's load balancer `request.client.host` is the proxy** unless proxy headers are trusted. Anything keyed on IP (signup dedup, rate limits) was silently wrong. `ProxyHeadersMiddleware(trusted_hosts="*")` in `create_app()` fixes it regardless of the start command.
- **Never `patch("module.threading.Thread")` in tests** — it patches `threading.Thread` globally (the attribute is the real module) and deadlocks anyio/ThreadPoolExecutor. Patch the worker function instead.
- **Charge after success, refund on failure.** Deducting before an external call (OpenAI, Google, Hunter) and not refunding is how every credit bug here started.
- **Never cache failures.** A cached "AI failed" score was sold for 24h to every user hitting that URL.
- **HTMX + 302**: a 401 handler that redirects makes HTMX swap the login page into the target element. Return `HX-Redirect` for `HX-Request` calls.
- **In-memory dicts are not state.** Anything a user can come back to (bulk selections, send jobs, batch progress) must live in the DB or degrade gracefully on restart.
- Local dev venv was Python 3.9 while the code uses 3.10 syntax — always check `python --version` against `render.yaml` before trusting a local run.
