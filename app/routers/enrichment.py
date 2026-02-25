import logging

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import Lead, UserAPIKeys
from app.services.credits import credit_manager
from app.services.email_enrichment import (
    extract_emails_from_website,
    choose_best_email,
    extract_domain,
    enrich_from_hunter,
)
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)
router = APIRouter(tags=["enrichment"])


@router.post("/api/enrich/website")
def enrich_from_website(
    request: Request,
    lead_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()]
    leads = db.query(Lead).filter(Lead.id.in_(ids), Lead.user_id == user.id).all() if ids else []

    results = []
    for lead in leads:
        if not lead.website:
            results.append({"lead": lead, "emails": [], "status": "no_website"})
            continue

        emails = extract_emails_from_website(lead.website)
        best = choose_best_email(emails)

        if best and not lead.email:
            lead.email = best
            lead.email_source = "website"
            lead.email_candidates = emails
            db.commit()

        results.append({"lead": lead, "emails": emails, "best": best, "status": "found" if emails else "none"})

    return templates.TemplateResponse(
        "partials/enrichment_results.html",
        {"request": request, "results": results},
    )


@router.post("/api/enrich/hunter")
def enrich_hunter(
    request: Request,
    lead_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()]
    leads = db.query(Lead).filter(Lead.id.in_(ids), Lead.user_id == user.id).all() if ids else []

    if not leads:
        return HTMLResponse('<div class="error-msg">No leads selected</div>')

    # Check credits (2 per lead for Hunter)
    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "sms_send", len(leads))
    if not has:
        return HTMLResponse(f'<div class="error-msg">Insufficient credits. Need {cost}, have {balance}</div>')

    # Get user's Hunter API key
    keys = db.query(UserAPIKeys).filter_by(user_id=user.id).first()
    hunter_key = decrypt(keys.hunter_api_key) if keys and keys.hunter_api_key else None

    results = []
    for lead in leads:
        domain = extract_domain(lead.website)
        if not domain:
            results.append({"lead": lead, "emails": [], "status": "no_domain"})
            continue

        hunter_result = enrich_from_hunter(domain, hunter_api_key=hunter_key)
        credit_manager.deduct_credits(db, user.id, "sms_send", 1, f"Hunter enrichment: {lead.name}")

        found_emails = hunter_result.get("emails", [])
        if found_emails and not lead.email:
            best = found_emails[0]["email"]
            lead.email = best
            lead.email_source = "hunter"
            lead.email_confidence = found_emails[0].get("confidence", 0)
            lead.email_candidates = [e["email"] for e in found_emails]
            db.commit()

        results.append({"lead": lead, "emails": found_emails, "status": "found" if found_emails else "none"})

    return templates.TemplateResponse(
        "partials/enrichment_results.html",
        {"request": request, "results": results},
    )
