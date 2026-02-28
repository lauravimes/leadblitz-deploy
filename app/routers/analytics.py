import logging

from fastapi import APIRouter, Request, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.deps import get_db, get_current_user
from app.models import Lead, Campaign, CreditTransaction

logger = logging.getLogger(__name__)
router = APIRouter(tags=["analytics"])


@router.get("/api/stats")
def dashboard_stats(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    uid = user.id

    total_leads = db.query(func.count(Lead.id)).filter(Lead.user_id == uid).scalar() or 0
    scored_leads = db.query(func.count(Lead.id)).filter(Lead.user_id == uid, Lead.score.isnot(None)).scalar() or 0
    avg_score = db.query(func.avg(Lead.score)).filter(Lead.user_id == uid, Lead.score.isnot(None)).scalar() or 0

    by_stage = dict(
        db.query(Lead.stage, func.count(Lead.id))
        .filter(Lead.user_id == uid)
        .group_by(Lead.stage)
        .all()
    )

    total_campaigns = db.query(func.count(Campaign.id)).filter(Campaign.user_id == uid).scalar() or 0

    emails_sent = (
        db.query(func.coalesce(func.sum(Lead.emails_sent_count), 0))
        .filter(Lead.user_id == uid)
        .scalar()
    )

    sms_sent = (
        db.query(func.count(CreditTransaction.id))
        .filter(CreditTransaction.user_id == uid, CreditTransaction.description.like("%SMS%"))
        .scalar() or 0
    )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "partials/stats_cards.html",
        {
            "request": request,
            "total_leads": total_leads,
            "scored_leads": scored_leads,
            "avg_score": round(avg_score, 1),
            "by_stage": by_stage,
            "total_campaigns": total_campaigns,
            "emails_sent": emails_sent,
            "sms_sent": sms_sent,
        },
    )
