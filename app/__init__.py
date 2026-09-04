import logging
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def _check_config() -> None:
    """Fail fast on configuration that would silently break security."""
    from app.config import get_settings

    s = get_settings()
    if s.session_secret in ("", "change-me", "change-me-to-a-64-byte-random-string"):
        raise RuntimeError(
            "SESSION_SECRET is not set. Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    if not s.encryption_key:
        logger.error("ENCRYPTION_KEY is not set — saving or reading SMTP/Twilio/Hunter credentials will fail")
    if s.stripe_secret_key and not s.stripe_webhook_secret:
        logger.error("STRIPE_WEBHOOK_SECRET is not set — Stripe webhooks will be rejected until it is configured")


def create_app() -> FastAPI:
    _check_config()

    app = FastAPI(title="LeadBlitz v2", docs_url=None, redoc_url=None)

    # Render terminates TLS at its load balancer and forwards X-Forwarded-For /
    # X-Forwarded-Proto. Trust them so request.client.host is the real visitor
    # (needed for signup IP dedup and rate limiting) and request.url.scheme is https.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    # Static files
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    # Templates (shared instance)
    app.state.templates = Jinja2Templates(directory=BASE_DIR / "templates")

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 401:
            # HTMX follows a 302 transparently and would swap the login page into
            # the target element. Tell it to navigate instead.
            if request.headers.get("HX-Request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=302)
        return app.state.templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
        )

    @app.get("/health", include_in_schema=False)
    def health():
        return PlainTextResponse("ok")

    # Routers
    from app.routers import (
        pages, auth, search, leads, scoring,
        credits, settings, email, enrichment, sms,
        csv, reports, analytics, admin, public_score,
    )

    app.include_router(public_score.router)
    app.include_router(pages.router)
    app.include_router(auth.router, prefix="/auth")
    app.include_router(search.router, prefix="/api")
    app.include_router(leads.router, prefix="/api")
    app.include_router(scoring.router, prefix="/api")
    app.include_router(credits.router)
    app.include_router(settings.router)
    app.include_router(email.router)
    app.include_router(enrichment.router)
    app.include_router(sms.router)
    app.include_router(csv.router)
    app.include_router(reports.router)
    app.include_router(analytics.router)
    app.include_router(admin.router)

    @app.on_event("startup")
    def _recover_and_start_workers():
        # Work that was mid-flight when the previous process died: release batch
        # scoring claims and resume interrupted CSV imports (state lives in the DB).
        try:
            from app.database import SessionLocal
            from app.routers.scoring import reset_stale_claims
            from app.services.csv_import import resume_pending_imports

            db = SessionLocal()
            try:
                released = reset_stale_claims(db)
            finally:
                db.close()
            resumed = resume_pending_imports()
            if released or resumed:
                logger.info("Startup recovery: released %s scoring claims, resumed %s import leads", released, resumed)
        except Exception:
            logger.exception("Startup recovery failed")

        try:
            from app.services.send_jobs import start_worker
            start_worker()
        except ImportError:
            pass
        except Exception:
            logger.exception("Could not start send-jobs worker")

    return app
