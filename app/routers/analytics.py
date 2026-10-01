import logging

from fastapi import APIRouter, Request, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.deps import get_db, get_current_user
from app.models import Lead, Campaign, LeadOutcome

logger = logging.getLogger(__name__)
router = APIRouter(tags=["analytics"])


@router.get("/api/stats")
def dashboard_stats(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    uid = user.id

    totals = (
        db.query(
            func.count(Lead.id),
            func.count(Lead.score),
            func.avg(Lead.score),
            func.coalesce(func.sum(Lead.emails_sent_count), 0),
            func.coalesce(func.sum(Lead.sms_sent_count), 0),
            func.count(Lead.email),
        )
        .filter(Lead.user_id == uid)
        .one()
    )
    total_leads, scored_leads, avg_score, emails_sent, sms_sent, with_email = totals

    by_stage = dict(
        db.query(Lead.stage, func.count(Lead.id))
        .filter(Lead.user_id == uid)
        .group_by(Lead.stage)
        .all()
    )

    total_campaigns = db.query(func.count(Campaign.id)).filter(Campaign.user_id == uid).scalar() or 0

    milestones = dict(db.query(LeadOutcome.stage, func.count(func.distinct(LeadOutcome.lead_id))).filter(LeadOutcome.user_id == uid).group_by(LeadOutcome.stage).all())
    won_values = db.query(Lead.deal_currency, func.sum(Lead.deal_value_cents)).filter(Lead.user_id == uid, Lead.stage == "won", Lead.deal_value_cents.isnot(None)).group_by(Lead.deal_currency).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "partials/stats_cards.html",
        {
            "request": request,
            "milestones": milestones, "won_values": won_values,
            "total_leads": total_leads or 0,
            "scored_leads": scored_leads or 0,
            "avg_score": round(float(avg_score or 0), 1),
            "by_stage": by_stage,
            "total_campaigns": total_campaigns,
            "emails_sent": int(emails_sent or 0),
            "sms_sent": int(sms_sent or 0),
            "with_email": with_email or 0,
        },
    )
