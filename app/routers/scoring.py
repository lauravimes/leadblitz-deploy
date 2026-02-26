import logging
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.config import get_settings
from app.models import Lead, Campaign
from app.services.scorer import score_website_hybrid
from app.services.technographics import classify_tech_health

logger = logging.getLogger(__name__)
router = APIRouter(tags=["scoring"])

# In-memory batch status tracker
_batch_status: dict[str, dict] = {}


def _batch_score_worker(lead_ids: list[str], user_id: int, batch_id: str):
    """Background thread that scores leads one by one."""
    from app.database import SessionLocal
    from app.services.credits import credit_manager

    db = SessionLocal()
    settings = get_settings()
    status = _batch_status[batch_id]

    try:
        for lid in lead_ids:
            lead = db.query(Lead).filter(Lead.id == lid, Lead.user_id == user_id).first()
            if not lead or not lead.website:
                status["skipped"] += 1
                continue

            # Deduct 1 credit per lead
            ok, balance = credit_manager.deduct_credits(db, user_id, "ai_scoring", description=f"Score: {lead.name}")
            if not ok:
                status["failed"] += 1
                status["status"] = "completed"
                status["error"] = f"Insufficient credits ({balance} available)"
                return

            try:
                result = score_website_hybrid(db=db, url=lead.website, api_key=settings.openai_api_key)
                lead.score = result.get("final_score", 0)
                lead.heuristic_score = result.get("heuristic_score", 0)
                lead.ai_score = result.get("ai_score", 0)
                lead.score_breakdown = result
                lead.technographics = result.get("technographics")
                lead.last_scored_at = datetime.now(timezone.utc)
                db.commit()
                status["scored"] += 1
            except Exception as e:
                logger.error(f"Batch score error for lead {lid}: {e}")
                status["failed"] += 1
    finally:
        db.close()
        status["status"] = "completed"


# Batch routes MUST be defined before /score/{lead_id} to avoid
# FastAPI matching "batch" as a lead_id path parameter.

@router.post("/score/batch")
def batch_score(
    request: Request,
    campaign_id: str = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    templates = request.app.state.templates

    q = db.query(Lead).filter(Lead.user_id == user.id, Lead.score.is_(None), Lead.website.isnot(None))
    if campaign_id:
        q = q.filter(Lead.campaign_id == campaign_id)

    leads = q.all()
    if not leads:
        return HTMLResponse('<span class="subtext">No unscored leads found.</span>')

    batch_id = str(uuid.uuid4())[:8]
    _batch_status[batch_id] = {
        "status": "in_progress",
        "total": len(leads),
        "scored": 0,
        "failed": 0,
        "skipped": 0,
    }

    lead_ids = [l.id for l in leads]
    thread = threading.Thread(
        target=_batch_score_worker,
        args=(lead_ids, user.id, batch_id),
        daemon=True,
    )
    thread.start()

    return templates.TemplateResponse(
        "partials/batch_progress.html",
        {"request": request, "batch_id": batch_id, "status": _batch_status[batch_id]},
    )


@router.get("/score/batch/{batch_id}/status")
def batch_score_status(batch_id: str, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    templates = request.app.state.templates

    status = _batch_status.get(batch_id)
    if not status:
        return HTMLResponse('<span class="subtext">Batch not found.</span>')

    return templates.TemplateResponse(
        "partials/batch_progress.html",
        {"request": request, "batch_id": batch_id, "status": status},
    )


@router.post("/score/{lead_id}")
def score_lead(lead_id: str, request: Request, db: Session = Depends(get_db)):
    from app.services.credits import credit_manager

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

    # Deduct 1 credit for AI scoring
    ok, balance = credit_manager.deduct_credits(db, user.id, "ai_scoring", description=f"Score: {lead.name}")
    if not ok:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": f"Insufficient credits ({balance} available, 1 needed)"}
        )

    result = score_website_hybrid(
        db=db,
        url=lead.website,
        api_key=settings.openai_api_key,
    )

    lead.score = result.get("final_score", 0)
    lead.heuristic_score = result.get("heuristic_score", 0)
    lead.ai_score = result.get("ai_score", 0)
    lead.score_breakdown = result
    lead.technographics = result.get("technographics")
    lead.last_scored_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(lead)

    # Determine response based on HX-Target
    hx_target = request.headers.get("HX-Target", "")

    if hx_target == "score-panel":
        # From lead detail page — return score breakdown panel
        return templates.TemplateResponse(
            "partials/score_detail.html",
            {"request": request, "lead": lead, "classify_tech_health": classify_tech_health},
        )
    else:
        # From lead list — return updated card
        return templates.TemplateResponse(
            "partials/lead_card.html", {"request": request, "lead": lead}
        )
