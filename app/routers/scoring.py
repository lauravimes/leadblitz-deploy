import logging
import threading
import time
import uuid

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.config import get_settings
from app.models import Lead
from app.services.credits import credit_manager
from app.services.lead_filters import apply_lead_filters
from app.services.scorer import score_website_hybrid, apply_score_to_lead, record_score_failure
from app.services.technographics import classify_tech_health

logger = logging.getLogger(__name__)
router = APIRouter(tags=["scoring"])

_CACHE_TTL = 3600
_CACHE_MAX = 200

# In-memory batch status tracker (progress only — the authoritative state is the
# leads table, which is why a restart loses the progress bar but never a credit).
_batch_status: dict[str, dict] = {}
_status_lock = threading.Lock()

CLAIM_MARKER = "scoring"


def _evict_old_status():
    now = time.time()
    stale = [k for k, v in _batch_status.items() if now - v.get("_created", 0) > _CACHE_TTL]
    for k in stale:
        _batch_status.pop(k, None)
    while len(_batch_status) > _CACHE_MAX:
        _batch_status.pop(next(iter(_batch_status)), None)


def reset_stale_claims(db: Session) -> int:
    """Called at startup: leads left claimed by a batch that died with the process
    are released so they can be scored again (import leads go back to 'queued')."""
    released = 0
    released += db.execute(
        update(Lead)
        .where(Lead.import_status == CLAIM_MARKER, Lead.score.is_(None), Lead.import_id.is_(None))
        .values(import_status=None)
    ).rowcount or 0
    released += db.execute(
        update(Lead)
        .where(Lead.import_status == CLAIM_MARKER, Lead.score.is_(None), Lead.import_id.isnot(None))
        .values(import_status="queued")
    ).rowcount or 0
    db.commit()
    return released


def score_lead_charged(db: Session, lead: Lead, user_id: int, api_key: str, force: bool = False) -> dict:
    """Charge → score → persist, refunding on failure. Returns the scorer result
    (with ``has_errors``/``error_message`` set when the credit was refunded)."""
    ok, balance = credit_manager.deduct_credits(db, user_id, "ai_scoring", description=f"Score: {lead.name}")
    if not ok:
        return {"has_errors": True, "insufficient_credits": True, "balance": balance,
                "error_message": f"Insufficient credits ({balance} available, 1 needed)"}

    try:
        result = score_website_hybrid(db=db, url=lead.website, api_key=api_key, use_cache=not force)
    except Exception as exc:
        logger.exception("Scoring crashed for lead %s", lead.id)
        result = {"has_errors": True, "error_message": "Scoring failed unexpectedly. Please try again.",
                  "errors": [str(exc)[:200]]}

    if result.get("has_errors"):
        credit_manager.refund_credits(db, user_id, "ai_scoring", description=f"Refund: could not score {lead.name}")
        record_score_failure(lead, result)
        if lead.import_id:
            lead.import_status = "unreachable"
        elif lead.import_status == CLAIM_MARKER:
            lead.import_status = None
        db.commit()
        return result

    apply_score_to_lead(lead, result)
    if lead.import_id:
        lead.import_status = "scored"
    elif lead.import_status == CLAIM_MARKER:
        lead.import_status = None
    db.commit()
    return result


def _batch_score_worker(lead_ids: list[str], user_id: int, batch_id: str):
    """Background thread that scores claimed leads one by one."""
    from app.database import SessionLocal

    settings = get_settings()
    status = _batch_status[batch_id]

    try:
        for lid in lead_ids:
            db = SessionLocal()
            try:
                lead = db.query(Lead).filter(Lead.id == lid, Lead.user_id == user_id).first()
                if not lead or not lead.website:
                    status["skipped"] += 1
                    continue

                result = score_lead_charged(db, lead, user_id, settings.openai_api_key)
                if result.get("insufficient_credits"):
                    # Release the remaining claims and stop.
                    status["error"] = f"Stopped: insufficient credits ({result.get('balance', 0)} left)"
                    remaining = lead_ids[lead_ids.index(lid):]
                    db.execute(
                        update(Lead)
                        .where(Lead.id.in_(remaining), Lead.import_status == CLAIM_MARKER)
                        .values(import_status=None)
                    )
                    db.commit()
                    status["skipped"] += len(remaining)
                    return
                if result.get("has_errors"):
                    status["failed"] += 1
                else:
                    status["scored"] += 1
                with _status_lock:
                    status["recently_scored_ids"].append(str(lid))
            except Exception as e:
                logger.error(f"Batch score error for lead {lid}: {e}")
                status["failed"] += 1
            finally:
                db.close()
    finally:
        status["status"] = "completed"


# Batch routes MUST be defined before /score/{lead_id} to avoid
# FastAPI matching "batch" as a lead_id path parameter.

@router.post("/score/batch")
def batch_score(
    request: Request,
    campaign_id: str = Form(None),
    import_id: str = Form(None),
    stage: str = Form(None),
    has_email: str = Form(None),
    q: str = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    templates = request.app.state.templates

    base = db.query(Lead.id).filter(
        Lead.user_id == user.id,
        Lead.score.is_(None),
        Lead.website.isnot(None),
        Lead.website != "",
        or_(Lead.import_status.is_(None), Lead.import_status.notin_([CLAIM_MARKER, "queued"])),
    )
    base = apply_lead_filters(base, stage=stage, campaign_id=campaign_id, import_id=import_id,
                              has_email=has_email, search=q)
    candidate_ids = [row[0] for row in base.all()]
    if not candidate_ids:
        return HTMLResponse('<span class="subtext">No unscored leads match the current filters.</span>')

    has, balance, _ = credit_manager.has_sufficient_credits(db, user.id, "ai_scoring", 1)
    if not has:
        return HTMLResponse(f'<div class="error-msg">Insufficient credits ({balance} available). Each score costs 1 credit.</div>')

    # Atomically claim the leads so a second click (or a single Score during the
    # batch) cannot score and charge the same lead twice.
    claimed = db.execute(
        update(Lead)
        .where(
            Lead.id.in_(candidate_ids),
            Lead.user_id == user.id,
            Lead.score.is_(None),
            or_(Lead.import_status.is_(None), Lead.import_status.notin_([CLAIM_MARKER, "queued"])),
        )
        .values(import_status=CLAIM_MARKER)
        .returning(Lead.id)
    ).fetchall()
    db.commit()
    lead_ids = [row[0] for row in claimed]
    if not lead_ids:
        return HTMLResponse('<span class="subtext">Those leads are already being scored.</span>')

    batch_id = str(uuid.uuid4())[:8]
    _evict_old_status()
    _batch_status[batch_id] = {
        "status": "in_progress",
        "total": len(lead_ids),
        "scored": 0,
        "failed": 0,
        "skipped": 0,
        "error": None,
        "recently_scored_ids": [],
        "user_id": user.id,
        "_created": time.time(),
    }

    thread = threading.Thread(
        target=_batch_score_worker,
        args=(lead_ids, user.id, batch_id),
        daemon=True,
    )
    thread.start()

    return templates.TemplateResponse(
        "partials/batch_progress.html",
        {"request": request, "batch_id": batch_id, "status": _batch_status[batch_id], "lead_ids": lead_ids},
    )


@router.get("/score/batch/{batch_id}/status")
def batch_score_status(batch_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    templates = request.app.state.templates

    status = _batch_status.get(batch_id)
    if not status or status.get("user_id") != user.id:
        return HTMLResponse(
            '<span class="subtext">This batch finished or the server restarted — '
            'reload the page to see updated scores.</span>'
        )

    # Pop recently scored lead IDs and render their updated cards as OOB swaps
    with _status_lock:
        scored_ids = status.get("recently_scored_ids", [])
        status["recently_scored_ids"] = []

    oob_cards = ""
    if scored_ids:
        scored_leads = db.query(Lead).filter(
            Lead.id.in_(scored_ids), Lead.user_id == user.id
        ).all()
        for lead in scored_leads:
            card_resp = templates.TemplateResponse(
                "partials/lead_card.html", {"request": request, "lead": lead}
            )
            card_html = card_resp.body.decode()
            card_html = card_html.replace(
                f'id="lead-{lead.id}"',
                f'id="lead-{lead.id}" hx-swap-oob="outerHTML:#lead-{lead.id}"',
                1,
            )
            oob_cards += card_html

    progress_resp = templates.TemplateResponse(
        "partials/batch_progress.html",
        {"request": request, "batch_id": batch_id, "status": status, "lead_ids": []},
    )
    progress_html = progress_resp.body.decode()

    response = HTMLResponse(progress_html + oob_cards)
    if scored_ids:
        response.headers["HX-Trigger"] = "creditsChanged"
    return response


@router.post("/score/{lead_id}")
def score_lead(
    lead_id: str,
    request: Request,
    force: str = Form(None),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    settings = get_settings()

    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
    if not lead:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": "Lead not found"}
        )

    if not lead.website:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": "No website to score"}
        )

    hx_target = request.headers.get("HX-Target", "")

    def render(score_error: str | None = None):
        if hx_target == "score-panel":
            resp = templates.TemplateResponse(
                "partials/score_detail.html",
                {"request": request, "lead": lead, "classify_tech_health": classify_tech_health,
                 "score_error": score_error},
            )
        else:
            resp = templates.TemplateResponse(
                "partials/lead_card.html", {"request": request, "lead": lead, "score_error": score_error}
            )
        resp.headers["HX-Trigger"] = "creditsChanged"
        return resp

    # Already scored and not an explicit re-score: don't charge again.
    if lead.score is not None and not force:
        return render()

    # Already claimed by a running batch
    if lead.import_status == CLAIM_MARKER:
        return render("This lead is being scored by a batch — check back in a moment.")

    result = score_lead_charged(db, lead, user.id, settings.openai_api_key, force=bool(force))
    db.refresh(lead)

    if result.get("insufficient_credits"):
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": result["error_message"]},
        )
    if result.get("has_errors"):
        return render(result.get("error_message", "Could not score this site — credit refunded."))

    return render()
