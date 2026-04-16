import logging
import threading
import uuid

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

# In-memory batch enrichment status tracker
_enrich_batch_status: dict[str, dict] = {}


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


def _batch_enrich_worker(lead_ids: list[str], user_id: int, batch_id: str):
    """Background thread that scrapes emails from lead websites using 10 concurrent workers."""
    import concurrent.futures
    from app.database import SessionLocal

    status = _enrich_batch_status[batch_id]
    CHUNK_SIZE = 50

    def _enrich_single(lid: str):
        # Step 1: brief DB read to get website
        db = SessionLocal()
        try:
            lead = db.query(Lead).filter(Lead.id == lid, Lead.user_id == user_id).first()
            if not lead:
                status["skipped"] += 1
                return
            website = lead.website
            if not website:
                lead.email_source = "no_website"
                db.commit()
                status["skipped"] += 1
                return
        except Exception as e:
            logger.error(f"Batch enrich read error for lead {lid}: {e}")
            status["failed"] += 1
            return
        finally:
            db.close()

        # Step 2: scrape website (no DB connection held)
        try:
            emails = extract_emails_from_website(website, timeout=8)
            best = choose_best_email(emails)
        except Exception as e:
            logger.error(f"Batch enrich scrape error for lead {lid}: {e}")
            status["failed"] += 1
            return

        # Step 3: brief DB write to save result
        db = SessionLocal()
        try:
            lead = db.query(Lead).filter(Lead.id == lid, Lead.user_id == user_id).first()
            if not lead:
                return
            if best:
                lead.email = best
                lead.email_source = "website"
                lead.email_candidates = emails
                status["found"] += 1
            else:
                lead.email_source = "scraped_none"
                status["not_found"] += 1
            db.commit()
            status["recently_enriched_ids"].append(str(lid))
        except Exception as e:
            logger.error(f"Batch enrich write error for lead {lid}: {e}")
            status["failed"] += 1
        finally:
            db.close()

    try:
        # Process in chunks with reduced concurrency to avoid exhausting the DB pool
        for i in range(0, len(lead_ids), CHUNK_SIZE):
            chunk = lead_ids[i:i + CHUNK_SIZE]
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(_enrich_single, lid) for lid in chunk]
                for future in concurrent.futures.as_completed(futures, timeout=300):
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Enrich future error: {e}")
                        status["failed"] += 1
    except Exception as e:
        logger.error(f"Batch enrich worker crashed: {e}")
    finally:
        status["status"] = "completed"


@router.post("/api/enrich/batch")
def batch_enrich(
    request: Request,
    campaign_id: str = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    templates = request.app.state.templates

    q = db.query(Lead).filter(
        Lead.user_id == user.id,
        Lead.email.is_(None),
        Lead.website.isnot(None),
        Lead.website != "",
        Lead.email_source.is_(None),  # skip leads already scraped with no result
    )
    if campaign_id:
        q = q.filter(Lead.campaign_id == campaign_id)

    leads = q.all()
    if not leads:
        return HTMLResponse('<span class="subtext">All leads already have emails (or no website to scrape).</span>')

    batch_id = str(uuid.uuid4())[:8]
    _enrich_batch_status[batch_id] = {
        "status": "in_progress",
        "total": len(leads),
        "found": 0,
        "not_found": 0,
        "failed": 0,
        "skipped": 0,
        "recently_enriched_ids": [],
    }

    lead_ids = [l.id for l in leads]
    thread = threading.Thread(
        target=_batch_enrich_worker,
        args=(lead_ids, user.id, batch_id),
        daemon=True,
    )
    thread.start()

    return templates.TemplateResponse(
        "partials/enrich_batch_progress.html",
        {"request": request, "batch_id": batch_id, "status": _enrich_batch_status[batch_id]},
    )


@router.get("/api/enrich/batch/{batch_id}/status")
def batch_enrich_status(batch_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    templates = request.app.state.templates

    status = _enrich_batch_status.get(batch_id)
    if not status:
        return HTMLResponse('<span class="subtext">Batch not found.</span>')

    # Pop recently enriched lead IDs and render updated cards as OOB swaps
    enriched_ids = status.get("recently_enriched_ids", [])
    status["recently_enriched_ids"] = []

    oob_cards = ""
    if enriched_ids:
        enriched_leads = db.query(Lead).filter(
            Lead.id.in_(enriched_ids), Lead.user_id == user.id
        ).all()
        for lead in enriched_leads:
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
        "partials/enrich_batch_progress.html",
        {"request": request, "batch_id": batch_id, "status": status},
    )
    progress_html = progress_resp.body.decode()

    return HTMLResponse(progress_html + oob_cards)
