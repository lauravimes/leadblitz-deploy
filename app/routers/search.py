import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.config import get_settings
from app.models import Campaign, Lead
from app.services.places import search_places, PageTokenExpired
from app.services.credits import credit_manager
from app.services.email_enrichment import extract_emails_from_website, choose_best_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

SCRAPE_DEADLINE_SECONDS = 25


def _auto_scrape_emails(leads: list, db: Session) -> None:
    """Scrape emails from lead websites in parallel and update DB. Never raises."""
    leads_with_sites = [l for l in leads if l.website]
    if not leads_with_sites:
        return

    def _scrape(lead_id: str, website: str):
        try:
            candidates = extract_emails_from_website(website, timeout=5)
            best = choose_best_email(candidates, own_domain=website)
            return (lead_id, best, candidates)
        except Exception:
            return (lead_id, None, [])

    found: dict[str, tuple] = {}
    executor = ThreadPoolExecutor(max_workers=6)
    try:
        futures = [executor.submit(_scrape, l.id, l.website) for l in leads_with_sites]
        try:
            for future in as_completed(futures, timeout=SCRAPE_DEADLINE_SECONDS):
                try:
                    lead_id, best, candidates = future.result(timeout=1)
                    if best:
                        found[lead_id] = (best, candidates)
                except Exception as e:
                    logger.debug(f"Email scrape failed: {e}")
        except FuturesTimeout:
            logger.info("Email scraping deadline reached; saving %d results so far", len(found))
    finally:
        # Don't block the request on stragglers.
        executor.shutdown(wait=False, cancel_futures=True)

    if not found:
        return
    try:
        for lead in leads_with_sites:
            hit = found.get(lead.id)
            if hit:
                lead.email = hit[0]
                lead.email_source = "website"
                lead.email_candidates = hit[1]
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to save scraped emails: {e}")


def _create_leads(db: Session, user_id: int, campaign: Campaign, places: list) -> list:
    """Insert leads that aren't already in the campaign. Returns the new Lead rows."""
    existing = db.query(Lead.google_place_id, Lead.website).filter(
        Lead.campaign_id == campaign.id
    ).all()
    existing_place_ids = {l.google_place_id for l in existing if l.google_place_id}
    existing_websites = {l.website for l in existing if l.website}

    leads = []
    for place in places:
        pid = place.get("place_id")
        if pid and pid in existing_place_ids:
            continue
        website = place.get("website") or ""
        # Only dedupe by website when there is no place id to go on (franchise
        # branches legitimately share one site).
        if website and not pid and website in existing_websites:
            continue
        lead = Lead(
            user_id=user_id,
            campaign_id=campaign.id,
            google_place_id=pid,
            name=place.get("name", ""),
            address=place.get("address", ""),
            phone=place.get("phone", ""),
            website=website,
            rating=place.get("rating", 0),
            review_count=place.get("review_count", 0),
        )
        db.add(lead)
        leads.append(lead)
        if pid:
            existing_place_ids.add(pid)
    db.commit()
    for lead in leads:
        db.refresh(lead)
    return leads


def _run_search(request: Request, db: Session, user, campaign: Campaign, page_token, description: str):
    """Shared by /search and /search/more: fetch a page, create leads, charge only
    when something new was found, scrape emails, and return leads + charge info."""
    settings = get_settings()

    try:
        result = search_places(
            api_key=settings.google_maps_api_key,
            business_type=campaign.business_type,
            location=campaign.location,
            page_token=page_token,
        )
    except PageTokenExpired:
        campaign.next_page_token = None
        db.commit()
        raise
    campaign.next_page_token = result.get("next_page_token")
    db.commit()

    leads = _create_leads(db, user.id, campaign, result["places"])

    charged = False
    if leads:
        ok, _balance = credit_manager.deduct_credits(db, user.id, "lead_search", 1, description)
        charged = ok
        if not ok:
            logger.warning("Search produced leads but credit deduction failed for user %s", user.id)

    _auto_scrape_emails(leads, db)
    for lead in leads:
        db.refresh(lead)

    return leads, result.get("next_page_token"), charged


def _error(request: Request, message: str):
    return request.app.state.templates.TemplateResponse(
        "partials/error.html", {"request": request, "message": message}
    )


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

    business_type = (business_type or "").strip()[:255]
    location = (location or "").strip()[:255]
    if not business_type or not location:
        return _error(request, "Enter both a business type and a location.")

    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "lead_search")
    if not has:
        return _error(request, f"Insufficient credits. Need {cost}, have {balance}")

    # Reuse an existing campaign for the same search so re-running it continues
    # from the next page instead of re-charging for duplicates.
    campaign = None
    if campaign_id:
        campaign = db.query(Campaign).filter(
            Campaign.id == campaign_id, Campaign.user_id == user.id
        ).first()
    if not campaign:
        campaign = db.query(Campaign).filter(
            Campaign.user_id == user.id,
            Campaign.business_type == business_type,
            Campaign.location == location,
        ).first()
    is_new_campaign = campaign is None
    if not campaign:
        campaign = Campaign(user_id=user.id, business_type=business_type, location=location)
        db.add(campaign)
        db.commit()
        db.refresh(campaign)

    page_token = None if is_new_campaign else campaign.next_page_token

    try:
        leads, next_token, charged = _run_search(
            request, db, user, campaign, page_token,
            f"Search: {campaign.business_type} in {campaign.location}",
        )
    except PageTokenExpired as exc:
        return _error(request, str(exc))
    except ValueError as exc:
        return _error(request, str(exc))

    exhausted = not leads and not next_token and not is_new_campaign
    response = templates.TemplateResponse(
        "partials/search_results.html",
        {
            "request": request,
            "leads": leads,
            "campaign_id": campaign.id,
            "next_page_token": next_token,
            "charged": charged,
            "exhausted": exhausted,
        },
    )
    if charged:
        response.headers["HX-Trigger"] = "creditsChanged"
    return response


@router.post("/search/more")
def search_more(
    request: Request,
    campaign_id: str = Form(...),
    next_page_token: str = Form(None),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)

    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id, Campaign.user_id == user.id
    ).first()
    if not campaign:
        return _error(request, "Campaign not found")

    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "lead_search")
    if not has:
        return _error(request, f"Insufficient credits. Need {cost}, have {balance}")

    # The server-side token is authoritative; the form value is only a fallback.
    token = campaign.next_page_token or next_page_token
    if not token:
        return HTMLResponse(
            '<div id="load-more" hx-swap-oob="true"><p class="subtext">No more results for this search.</p></div>'
        )

    try:
        leads, next_token, charged = _run_search(
            request, db, user, campaign, token,
            f"Search more: {campaign.business_type} in {campaign.location}",
        )
    except PageTokenExpired as exc:
        return _error(request, str(exc))
    except ValueError as exc:
        return _error(request, str(exc))

    list_html = templates.TemplateResponse(
        "partials/lead_list.html", {"request": request, "leads": leads}
    ).body.decode()
    more_html = templates.TemplateResponse(
        "partials/load_more.html",
        {"request": request, "campaign_id": campaign.id, "next_page_token": next_token,
         "new_count": len(leads), "oob": True},
    ).body.decode()

    response = HTMLResponse(list_html + more_html)
    if charged:
        response.headers["HX-Trigger"] = "creditsChanged"
    return response


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
