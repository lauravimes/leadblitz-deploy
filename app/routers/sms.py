import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import Lead, UserAPIKeys
from app.services.credits import credit_manager
from app.services.sms import send_sms, prepare_sms_variables, render_sms_template
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sms"])


@router.post("/api/sms/preview")
def preview_sms(
    request: Request,
    template: str = Form(""),
    lead_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()][:5]
    leads = db.query(Lead).filter(Lead.id.in_(ids), Lead.user_id == user.id).all() if ids else []

    previews = []
    for lead in leads:
        variables = prepare_sms_variables({
            "name": lead.name, "address": lead.address,
            "score": lead.score, "phone": lead.phone, "website": lead.website,
        })
        rendered = render_sms_template(template, variables)
        previews.append({"lead": lead, "message": rendered})

    return templates.TemplateResponse(
        "partials/sms_preview.html",
        {"request": request, "previews": previews},
    )


@router.post("/api/sms/send")
def send_sms_bulk(
    request: Request,
    template: str = Form(...),
    lead_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()]
    leads = db.query(Lead).filter(Lead.id.in_(ids), Lead.user_id == user.id).all() if ids else []

    leads_with_phone = [l for l in leads if l.phone]
    if not leads_with_phone:
        return HTMLResponse('<div class="error-msg">No leads with phone numbers selected</div>')

    # Check credits (2 per SMS)
    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "sms_send", len(leads_with_phone))
    if not has:
        return HTMLResponse(f'<div class="error-msg">Insufficient credits. Need {cost}, have {balance}</div>')

    # Get Twilio keys
    keys = db.query(UserAPIKeys).filter_by(user_id=user.id).first()
    if not keys or not keys.twilio_account_sid:
        return HTMLResponse('<div class="error-msg">Twilio not configured. Set up API keys in Settings.</div>')

    account_sid = keys.twilio_account_sid
    auth_token = keys.twilio_auth_token
    phone_number = keys.twilio_phone_number

    sent = 0
    errors = []
    for lead in leads_with_phone:
        variables = prepare_sms_variables({
            "name": lead.name, "address": lead.address,
            "score": lead.score, "phone": lead.phone, "website": lead.website,
        })
        message = render_sms_template(template, variables)
        result = send_sms(lead.phone, message, account_sid, auth_token, phone_number)

        if result["success"]:
            credit_manager.deduct_credits(db, user.id, "sms_send", 1, f"SMS to {lead.phone}")
            lead.last_sms_at = datetime.now(timezone.utc)
            lead.sms_sent_count = (lead.sms_sent_count or 0) + 1
            sent += 1
        else:
            errors.append(f"{lead.name}: {result.get('error', 'Unknown error')}")

    msg = f"Sent {sent} SMS"
    if errors:
        msg += f". {len(errors)} failed."
    return HTMLResponse(f'<span class="saved-flash">{msg}</span>')
