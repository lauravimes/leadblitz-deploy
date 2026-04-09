"""Public /score page — free website scoring, no auth required."""

import logging
import time
from collections import defaultdict

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_optional_user
from app.config import get_settings
from app.services.scorer import score_website_hybrid, normalize_url
from app.services.technographics import classify_tech_health

logger = logging.getLogger(__name__)
router = APIRouter(tags=["public-score"])

# Simple in-memory rate limiter: IP -> list of timestamps
_rate_limits: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 5  # max scores per IP per hour
RATE_WINDOW = 3600  # seconds


def _check_rate_limit(ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now = time.time()
    timestamps = _rate_limits[ip]
    # Prune old entries
    _rate_limits[ip] = [t for t in timestamps if now - t < RATE_WINDOW]
    if len(_rate_limits[ip]) >= RATE_LIMIT:
        return False
    _rate_limits[ip].append(now)
    return True


@router.get("/score")
def score_page(request: Request, db: Session = Depends(get_db)):
    user = get_optional_user(request, db)
    return request.app.state.templates.TemplateResponse(
        "pages/score.html", {"request": request, "user": user}
    )


@router.post("/score")
def score_url(
    request: Request,
    url: str = Form(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    settings = get_settings()
    user = get_optional_user(request, db)

    # Normalize and validate
    url = url.strip()
    if not url:
        return HTMLResponse(
            '<div class="card" style="text-align:center"><p style="color:var(--red)">Please enter a URL.</p></div>'
        )

    normalized = normalize_url(url)
    if not normalized or "." not in normalized:
        return HTMLResponse(
            '<div class="card" style="text-align:center"><p style="color:var(--red)">Please enter a valid website URL.</p></div>'
        )

    # Rate limit by IP (skip for authenticated users)
    if not user:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            return HTMLResponse(
                '<div class="card" style="text-align:center">'
                '<p style="color:var(--red)">Rate limit reached. Sign up free to get unlimited scores.</p>'
                '<a href="/register" class="btn btn-primary btn-sm" style="margin-top:12px">Sign up free</a>'
                '</div>'
            )

    # Run the scoring pipeline — no credits deducted
    try:
        result = score_website_hybrid(
            db=db,
            url=normalized,
            api_key=settings.openai_api_key,
        )
    except Exception as e:
        logger.exception("Public score failed for %s", url)
        return HTMLResponse(
            '<div class="card" style="text-align:center">'
            '<p style="color:var(--red)">Something went wrong scoring that site. Please try again.</p>'
            '</div>'
        )

    return templates.TemplateResponse(
        "partials/public_score_result.html",
        {
            "request": request,
            "url": normalized,
            "result": result,
            "user": user,
            "classify_tech_health": classify_tech_health,
        },
    )
