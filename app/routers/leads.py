import uuid as _uuid

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import Lead

router = APIRouter(tags=["leads"])

# Short-lived server-side storage for bulk lead selections.
# Keyed by 12-char token → {"user_id": int, "lead_ids": list[str], "attach_report": bool}
_bulk_selections: dict[str, dict] = {}
_BULK_SELECTIONS_MAX = 100


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

    # Evict oldest entries if cache is full
    while len(_bulk_selections) >= _BULK_SELECTIONS_MAX:
        oldest_key = next(iter(_bulk_selections))
        _bulk_selections.pop(oldest_key, None)

    token = str(_uuid.uuid4()).replace("-", "")[:12]
    _bulk_selections[token] = {
        "user_id": user.id,
        "lead_ids": ids,
        "attach_report": attach_report == "1",
    }

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

    # Evict oldest entries if cache is full
    while len(_bulk_selections) >= _BULK_SELECTIONS_MAX:
        oldest_key = next(iter(_bulk_selections))
        _bulk_selections.pop(oldest_key, None)

    token = str(_uuid.uuid4()).replace("-", "")[:12]
    _bulk_selections[token] = {
        "user_id": user.id,
        "lead_ids": ids,
    }

    response = Response(status_code=200)
    response.headers["HX-Redirect"] = f"/sms?bulk_token={token}"
    return response
