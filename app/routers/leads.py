import secrets
import time

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import Lead
from app.services.lead_filters import apply_lead_filters

router = APIRouter(tags=["leads"])

# Short-lived server-side storage for bulk lead selections.
# Keyed by token → {"user_id": int, "lead_ids": list[str], "attach_report": bool, "_created": float}
# Entries are NOT single-use (a page refresh must not lose the selection); they
# expire after _BULK_TTL seconds instead.
_bulk_selections: dict[str, dict] = {}
_BULK_SELECTIONS_MAX = 500
_BULK_TTL = 3600


def store_bulk_selection(user_id: int, lead_ids: list[str], attach_report: bool = False) -> str:
    now = time.time()
    for key in [k for k, v in _bulk_selections.items() if now - v.get("_created", 0) > _BULK_TTL]:
        _bulk_selections.pop(key, None)
    while len(_bulk_selections) >= _BULK_SELECTIONS_MAX:
        _bulk_selections.pop(next(iter(_bulk_selections)), None)
    token = secrets.token_urlsafe(12)
    _bulk_selections[token] = {
        "user_id": user_id,
        "lead_ids": lead_ids,
        "attach_report": attach_report,
        "_created": now,
    }
    return token


def get_bulk_selection(token: str, user_id: int) -> dict | None:
    sel = _bulk_selections.get(token)
    if not sel or sel["user_id"] != user_id:
        return None
    if time.time() - sel.get("_created", 0) > _BULK_TTL:
        _bulk_selections.pop(token, None)
        return None
    return sel


@router.patch("/leads/{lead_id}/stage")
def update_stage(
    lead_id: str,
    request: Request,
    stage: str = Form(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)

    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
    if not lead:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": "Lead not found"}
        )

    if stage not in ("new", "reviewing", "qualified", "rejected"):
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": "Invalid stage"}
        )

    lead.stage = stage
    db.commit()
    db.refresh(lead)

    # If request came from lead detail page (stage-confirm target), return flash
    hx_target = request.headers.get("HX-Target", "")
    if hx_target == "stage-confirm":
        return HTMLResponse('<span class="saved-flash">Saved</span>')

    # Otherwise return updated card
    return templates.TemplateResponse(
        "partials/lead_card.html", {"request": request, "lead": lead}
    )


@router.patch("/leads/{lead_id}/notes")
def update_notes(
    lead_id: str,
    request: Request,
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)

    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
    if not lead:
        return HTMLResponse("")

    lead.notes = notes
    db.commit()

    return HTMLResponse('<span class="saved-flash">Saved</span>')


@router.patch("/leads/{lead_id}/contact")
def update_contact(
    lead_id: str,
    request: Request,
    phone: str = Form(""),
    email: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)

    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
    if not lead:
        return HTMLResponse("")

    lead.phone = phone.strip() or None
    lead.email = email.strip() or None
    db.commit()

    return HTMLResponse('<span class="saved-flash">Saved</span>')


@router.delete("/leads/{lead_id}")
def delete_lead(
    lead_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)

    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
    if lead:
        db.delete(lead)
        db.commit()

    # HX-Redirect back to leads list
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/leads"
    return response


@router.post("/leads/email-all")
def email_all_filtered(
    request: Request,
    campaign_id: str = Form(None),
    import_id: str = Form(None),
    stage: str = Form(None),
    has_email: str = Form(None),
    scored: str = Form(None),
    q: str = Form(None),
    attach_report: str = Form("0"),
    db: Session = Depends(get_db),
):
    """Email all leads matching the CURRENT filters (not just the current page).
    Uses the same filter helper as the leads page so the set matches what the
    user is looking at."""
    user = get_current_user(request, db)

    query = db.query(Lead.id).filter(Lead.user_id == user.id, Lead.email.isnot(None), Lead.email != "")
    query = apply_lead_filters(
        query, stage=stage, campaign_id=campaign_id, import_id=import_id,
        scored=scored, has_email=has_email, search=q,
    )

    ids = [str(row[0]) for row in query.all()]
    if not ids:
        return HTMLResponse('<div class="error-msg">No leads with an email address match the current filters</div>')

    token = store_bulk_selection(user.id, ids, attach_report == "1")
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = f"/email?bulk_token={token}"
    return response


@router.post("/leads/bulk-email")
def bulk_email_redirect(
    request: Request,
    lead_ids: str = Form(""),
    attach_report: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)

    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()]
    if not ids:
        return HTMLResponse('<div class="error-msg">No leads selected</div>')

    token = store_bulk_selection(user.id, ids, attach_report == "1")
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = f"/email?bulk_token={token}"
    return response


@router.post("/leads/bulk-sms")
def bulk_sms_redirect(
    request: Request,
    lead_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)

    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()]
    if not ids:
        return HTMLResponse('<div class="error-msg">No leads selected</div>')

    token = store_bulk_selection(user.id, ids)
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = f"/sms?bulk_token={token}"
    return response


@router.post("/leads/bulk-delete")
def bulk_delete(
    request: Request,
    lead_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)

    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()]
    if not ids:
        return HTMLResponse('<div class="error-msg">No leads selected</div>')

    deleted = db.query(Lead).filter(Lead.id.in_(ids), Lead.user_id == user.id).delete(
        synchronize_session=False
    )
    db.commit()

    count_text = f"Deleted {deleted} lead{'s' if deleted != 1 else ''}."
    response = HTMLResponse(f'<span class="saved-flash">{count_text}</span>')
    response.headers["HX-Trigger"] = "leadsDeleted"
    return response
