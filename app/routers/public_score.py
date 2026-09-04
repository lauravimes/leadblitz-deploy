"""Public /score page — free website scoring, no auth required."""

import logging
import threading
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_optional_user
from app.config import get_settings
from app.services.scorer import score_website_hybrid, normalize_url
from app.services.technographics import classify_tech_health
from app.services.pagespeed import fetch_mobile_speed
from app.services.rate_limit import (
    client_ip,
    public_score_ip_limiter,
    public_score_user_limiter,
    pagespeed_limiter,
)
from app.services.url_safety import is_safe_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["public-score"])

# PageSpeed results are expensive (up to 30s, shared Google quota) and stable for
# hours. Cache per normalised URL in-process.
_PAGESPEED_TTL = 6 * 3600
_PAGESPEED_MAX = 500
_pagespeed_cache: dict[str, tuple[float, dict | None]] = {}
_pagespeed_lock = threading.Lock()


def _pagespeed_cached(url: str, api_key: str):
    now = time.time()
    with _pagespeed_lock:
        hit = _pagespeed_cache.get(url)
        if hit and now - hit[0] < _PAGESPEED_TTL:
            return hit[1]
    result = fetch_mobile_speed(url, api_key)
    with _pagespeed_lock:
        if len(_pagespeed_cache) >= _PAGESPEED_MAX:
            oldest = min(_pagespeed_cache, key=lambda k: _pagespeed_cache[k][0])
            _pagespeed_cache.pop(oldest, None)
        _pagespeed_cache[url] = (now, result)
    return result


def _msg(text: str, cta: bool = False) -> HTMLResponse:
    html = f'<div class="card" style="text-align:center"><p style="color:var(--red)">{text}</p>'
    if cta:
        html += '<a href="/register" class="btn btn-primary btn-sm" style="margin-top:12px">Sign up free</a>'
    html += "</div>"
    return HTMLResponse(html)


def _looks_like_website(normalized: str) -> bool:
    host = urlparse(normalized).hostname or ""
    return "." in host and " " not in host and len(host) <= 253


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

    url = (url or "").strip()
    if not url:
        return _msg("Please enter a URL.")

    normalized = normalize_url(url)
    if not normalized or not _looks_like_website(normalized):
        return _msg("Please enter a valid website URL.")

    # Private / internal addresses are never fetched from the server.
    if not is_safe_url(normalized):
        return _msg("That address can't be scored. Please enter a public website URL.")

    # Rate limit: anonymous visitors by IP, signed-in users by account. The free
    # tool costs real OpenAI money on every uncached call.
    if user:
        if not public_score_user_limiter.allow(f"pubscore:user:{user.id}"):
            return _msg("You've used the free scorer a lot this hour. Score leads from your dashboard instead.")
    else:
        if not public_score_ip_limiter.allow(f"pubscore:ip:{client_ip(request)}"):
            return _msg("Rate limit reached. Sign up free to get more scores.", cta=True)

    try:
        result = score_website_hybrid(
            db=db,
            url=normalized,
            api_key=settings.openai_api_key,
        )
    except Exception:
        logger.exception("Public score failed for %s", url)
        return _msg("Something went wrong scoring that site. Please try again.")

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


@router.get("/api/pagespeed")
def pagespeed_check(request: Request, url: str = Query(...), db: Session = Depends(get_db)):
    """Lazy-loaded PageSpeed endpoint — called via HTMX after initial score renders."""
    templates = request.app.state.templates
    settings = get_settings()

    normalized = normalize_url(url)
    if not normalized or not _looks_like_website(normalized) or not is_safe_url(normalized):
        return HTMLResponse("")

    user = get_optional_user(request, db)
    key = f"pagespeed:user:{user.id}" if user else f"pagespeed:ip:{client_ip(request)}"
    if not pagespeed_limiter.allow(key):
        return HTMLResponse("")

    pagespeed = _pagespeed_cached(normalized, settings.google_maps_api_key)
    if not pagespeed:
        return HTMLResponse("")

    return templates.TemplateResponse(
        "partials/pagespeed_card.html",
        {"request": request, "pagespeed": pagespeed},
    )
