"""Hybrid scoring orchestrator — adapted from original, uses db session as param."""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

from sqlalchemy.orm import Session

from app.models import ScoreCache

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunparse((parsed.scheme, netloc, parsed.path.rstrip("/") or "/", "", "", ""))


def url_to_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def get_cached_score(db: Session, url: str, max_age_hours: int = 24) -> Optional[Dict[str, Any]]:
    normalized = normalize_url(url)
    url_hash = url_to_hash(normalized)

    entry = db.query(ScoreCache).filter(ScoreCache.url_hash == url_hash).first()
    if not entry:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    fetched = entry.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if fetched < cutoff:
        return None

    heuristic_data = entry.heuristic_result or {}
    ai_data = entry.ai_result or {}
    ai_scores = ai_data.get("category_scores", {})

    return {
        "final_score": entry.final_score,
        "confidence": entry.confidence,
        "heuristic_score": heuristic_data.get("total_heuristic", 0),
        "ai_score": int(min(50, sum(ai_scores.values()))),
        "breakdown": {
            "heuristic": heuristic_data.get("scores", {}),
            "ai": ai_scores,
        },
        "evidence": heuristic_data.get("evidence", {}),
        "ai_justifications": ai_data.get("justifications", {}),
        "plain_english_report": ai_data.get("plain_english_report", {}),
        "rendering_limitations": heuristic_data.get("rendering_limitations", False),
        "cached": True,
    }


def save_score_to_cache(db: Session, url: str, score_data: Dict[str, Any]) -> None:
    normalized = normalize_url(url)
    url_hash = url_to_hash(normalized)

    entry = db.query(ScoreCache).filter(ScoreCache.url_hash == url_hash).first()
    if entry:
        entry.heuristic_result = score_data.get("heuristic")
        entry.ai_result = score_data.get("ai_review")
        entry.final_score = score_data.get("final_score", 0)
        entry.confidence = score_data.get("confidence", 0.5)
        entry.fetched_at = datetime.now(timezone.utc)
    else:
        entry = ScoreCache(
            url_hash=url_hash,
            normalized_url=normalized,
            heuristic_result=score_data.get("heuristic"),
            ai_result=score_data.get("ai_review"),
            final_score=score_data.get("final_score", 0),
            confidence=score_data.get("confidence", 0.5),
        )
        db.add(entry)

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to cache score for %s", url)


def score_website_hybrid(db: Session, url: str, api_key: str, use_cache: bool = True) -> Dict[str, Any]:
    """
    Simplified scoring pipeline (no Playwright — v2 is static-only):
    1. Check cache
    2. Fetch static HTML (multi-page)
    3. Detect JS frameworks
    4. Heuristic scoring
    5. Technographics detection
    6. AI scoring
    7. Combine + cache
    """
    from app.services.site_fetcher import fetch_multiple_pages, extract_site_content_for_ai
    from app.services.site_heuristics import score_site_heuristics
    from app.services.ai_scorer import score_with_ai, combine_scores
    from app.services.framework_detector import detect_js_framework, get_detection_summary
    from app.services.technographics import detect_technographics

    # 1. Cache check
    if use_cache:
        cached = get_cached_score(db, url)
        if cached:
            return cached

    # 2. Fetch
    fetch_result = fetch_multiple_pages(url, max_pages=3)
    final_url = fetch_result.get("final_url", url)
    static_html = fetch_result.get("combined_html", "")
    fetch_status = fetch_result.get("status")

    is_blocked = fetch_status in [403, 401, 429]

    if not static_html or is_blocked:
        return {
            "final_score": 0,
            "confidence": 0.3,
            "heuristic_score": 0,
            "ai_score": 0,
            "breakdown": {},
            "has_errors": True,
            "errors": fetch_result.get("errors", []),
            "rendering_limitations": True,
            "bot_blocked": is_blocked,
            "plain_english_report": {
                "strengths": ["Website has security measures in place"] if is_blocked else [],
                "weaknesses": [],
                "technology_observations": "Unable to access website for analysis",
                "sales_opportunities": [],
            },
            "cached": False,
        }

    # 3. Framework detection
    detection = detect_js_framework(static_html)
    logger.info("Framework detection for %s: %s", url, get_detection_summary(detection))

    # 4. Heuristic scoring
    heuristic = score_site_heuristics(static_html, final_url)

    # 5. Technographics
    technographics_data = detect_technographics(static_html, final_url)

    rendering_limitations = (
        heuristic.get("rendering_limitations", False)
        or (detection.get("is_js_heavy", False))
    )

    # 6. AI scoring
    site_content = extract_site_content_for_ai(static_html, max_chars=6000)
    ai_review = score_with_ai(
        api_key=api_key,
        site_content=site_content,
        heuristic_evidence=heuristic.get("evidence", {}),
        final_url=final_url,
        rendering_limitations=rendering_limitations,
        technographics=technographics_data,
    )

    # 7. Combine
    result = combine_scores(heuristic, ai_review)
    result["cached"] = False
    result["has_errors"] = False
    result["errors"] = fetch_result.get("errors", [])
    result["js_detected"] = detection.get("is_js_heavy", False)
    result["framework_hints"] = detection.get("framework_hints", [])
    result["technographics"] = technographics_data

    # 8. Cache
    if use_cache:
        cache_data = {
            "heuristic": heuristic,
            "ai_review": ai_review,
            "final_score": result["final_score"],
            "confidence": result["confidence"],
        }
        save_score_to_cache(db, url, cache_data)

    return result
