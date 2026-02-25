from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.config import get_settings
from app.models import Lead
from app.services.scorer import score_website_hybrid
from app.services.technographics import classify_tech_health

router = APIRouter(tags=["scoring"])


@router.post("/score/{lead_id}")
def score_lead(lead_id: str, request: Request, db: Session = Depends(get_db)):
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
