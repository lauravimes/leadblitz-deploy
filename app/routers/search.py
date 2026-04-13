from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.deps import get_db, get_current_user
from app.config import get_settings
from app.models import User, Campaign, Lead
from app.services.places import search_places
from app.services.credits import credit_manager
from app.services.email_enrichment import extract_emails_from_website, choose_best_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


def _auto_scrape_emails(leads: list, db: Session) -> None:
    """Scrape emails from lead websites in parallel and update DB."""
    leads_with_sites = [l for l in leads if l.website]
    if not leads_with_sites:
        return

    def _scrape(lead):
        try:
            candidates = extract_emails_from_website(lead.website, timeout=8)
            best = choose_best_email(candidates)
            return (lead.id, best, candidates)
        except Exception:
            return (lead.id, None, [])

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(_scrape, l) for l in leads_with_sites]
        for future in as_completed(futures, timeout=30):
            try:
                lead_id, best, candidates = future.result(timeout=10)
                if best:
                    lead = db.query(Lead).get(lead_id)
                    if lead:
                        lead.email = best
                        lead.email_source = "website"
                        lead.email_candidates = candidates
            except Exception as e:
                logger.debug(f"Email scrape failed: {e}")
    try:
        db.commit()
    except Exception as e:
        logger.error(f"Failed to save scraped emails: {e}")


@router.post("/search")
def search(
    request: Request,
    business_type: str = Form(...),
    location: str = Form(...),
    campaign_id: str = Form(None),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    settings = get_settings()

    # Check credits
    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "lead_search")
    if not has:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": f"Insufficient credits. Need {cost}, have {balance}"}
        )

    # Reuse existing campaign or create new one
    campaign = None
    if campaign_id:
        campaign = db.query(Campaign).filter(
            Campaign.id == campaign_id, Campaign.user_id == user.id
        ).first()

    if not campaign:
        # Check for existing campaign with same search
        campaign = db.query(Campaign).filter(
            Campaign.user_id == user.id,
            Campaign.business_type == business_type,
            Campaign.location == location,
        ).first()

    if not campaign:
        campaign = Campaign(
            user_id=user.id,
            business_type=business_type,
            location=location,
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)

    try:
        result = search_places(
            api_key=settings.google_maps_api_key,
            business_type=business_type,
            location=location,
            page_token=campaign.next_page_token if campaign_id else None,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": str(exc)}
        )

    # Save next_page_token
    campaign.next_page_token = result.get("next_page_token")
    db.commit()

    # Create leads (skip duplicates by google_place_id or website within campaign)
    existing = db.query(Lead.google_place_id, Lead.website).filter(
        Lead.campaign_id == campaign.id
    ).all()
    existing_place_ids = {l.google_place_id for l in existing if l.google_place_id}
    existing_websites = {l.website for l in existing if l.website}

    leads = []
    for place in result["places"]:
        pid = place.get("place_id")
        if pid and pid in existing_place_ids:
            continue
        if place.get("website") and place["website"] in existing_websites:
            continue
        lead = Lead(
            user_id=user.id,
            campaign_id=campaign.id,
            google_place_id=pid,
            name=place.get("name", ""),
            address=place.get("address", ""),
            phone=place.get("phone", ""),
            website=place.get("website", ""),
            rating=place.get("rating", 0),
            review_count=place.get("review_count", 0),
        )
        db.add(lead)
        leads.append(lead)
    db.commit()

    credit_manager.deduct_credits(db, user.id, "lead_search", 1, f"Search: {business_type} in {location}")

    # Refresh to get IDs
    for lead in leads:
        db.refresh(lead)

    # Auto-scrape emails from websites
    _auto_scrape_emails(leads, db)
    for lead in leads:
        db.refresh(lead)

    return templates.TemplateResponse(
        "partials/search_results.html",
        {
            "request": request,
            "leads": leads,
            "campaign_id": campaign.id,
            "next_page_token": result.get("next_page_token"),
        },
    )


@router.post("/search/more")
def search_more(
    request: Request,
    campaign_id: str = Form(...),
    next_page_token: str = Form(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    settings = get_settings()

    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id, Campaign.user_id == user.id
    ).first()
    if not campaign:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": "Campaign not found"}
        )

    # Check credits
    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "lead_search")
    if not has:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": f"Insufficient credits. Need {cost}, have {balance}"}
        )

    try:
        result = search_places(
            api_key=settings.google_maps_api_key,
            business_type=campaign.business_type,
            location=campaign.location,
            page_token=next_page_token,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": str(exc)}
        )

    campaign.next_page_token = result.get("next_page_token")
    db.commit()

    existing = db.query(Lead.google_place_id, Lead.website).filter(
        Lead.campaign_id == campaign.id
    ).all()
    existing_place_ids = {l.google_place_id for l in existing if l.google_place_id}
    existing_websites = {l.website for l in existing if l.website}

    leads = []
    for place in result["places"]:
        pid = place.get("place_id")
        if pid and pid in existing_place_ids:
            continue
        if place.get("website") and place["website"] in existing_websites:
            continue
        lead = Lead(
            user_id=user.id,
            campaign_id=campaign.id,
            google_place_id=pid,
            name=place.get("name", ""),
            address=place.get("address", ""),
            phone=place.get("phone", ""),
            website=place.get("website", ""),
            rating=place.get("rating", 0),
            review_count=place.get("review_count", 0),
        )
        db.add(lead)
        leads.append(lead)
    db.commit()

    credit_manager.deduct_credits(db, user.id, "lead_search", 1, f"Search more: {campaign.business_type} in {campaign.location}")

    for lead in leads:
        db.refresh(lead)

    # Auto-scrape emails from websites
    _auto_scrape_emails(leads, db)
    for lead in leads:
        db.refresh(lead)

    return templates.TemplateResponse(
        "partials/lead_list.html", {"request": request, "leads": leads}
    )


@router.get("/campaigns")
def list_campaigns(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaigns = (
        db.query(Campaign)
        .filter(Campaign.user_id == user.id)
        .order_by(Campaign.created_at.desc())
        .all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "partials/campaign_list.html",
        {"request": request, "campaigns": campaigns},
    )


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id, Campaign.user_id == user.id
    ).first()
    if not campaign:
        return HTMLResponse('<div class="error-msg">Campaign not found</div>', status_code=404)

    db.query(Lead).filter(Lead.campaign_id == campaign_id).delete()
    db.delete(campaign)
    db.commit()
    return HTMLResponse('<span class="saved-flash">Campaign deleted</span>')
