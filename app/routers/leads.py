from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import Lead

router = APIRouter(tags=["leads"])


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
    from fastapi.responses import Response
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/leads"
    return response
