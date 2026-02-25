from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    app = FastAPI(title="LeadBlitz v2", docs_url=None, redoc_url=None)

    # Static files
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    # Templates (shared instance)
    app.state.templates = Jinja2Templates(directory=BASE_DIR / "templates")

    # 401 → redirect to /login for page requests
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=302)
        return app.state.templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
        )

    # Routers
    from app.routers import (
        pages, auth, search, leads, scoring,
        credits, settings, email, enrichment, sms,
        csv, reports, analytics, admin,
    )

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

    return app
