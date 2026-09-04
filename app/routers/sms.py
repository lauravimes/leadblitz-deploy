import html
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import Lead, UserAPIKeys
from app.services.credits import credit_manager
from app.services.encryption import decrypt
from app.services.merge_fields import lead_merge_fields, render_merge_fields
from app.services.sms import infer_region, normalize_phone, send_sms, sms_segments

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sms"])

_REGIONS = {"", "GB", "US"}


def _error(message: str) -> HTMLResponse:
    return HTMLResponse(f'<div class="error-msg">{html.escape(message)}</div>')


def _user_leads(db: Session, user_id: int, lead_ids: str, limit: int | None = None) -> list[Lead]:
    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()]
    if limit:
        ids = ids[:limit]
    if not ids:
        return []
    return db.query(Lead).filter(Lead.id.in_(ids), Lead.user_id == user_id).all()


@router.post("/api/sms/preview")
def preview_sms(
    request: Request,
    template: str = Form(""),
    lead_ids: str = Form(""),
    region: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    leads = _user_leads(db, user.id, lead_ids, limit=5)
    region = region.upper() if region.upper() in _REGIONS else ""

    previews = []
    for lead in leads:
        message = render_merge_fields(template, lead_merge_fields(lead))
        to_phone = normalize_phone(lead.phone, region or infer_region(lead.phone, lead.address))
        previews.append({
            "lead": lead,
            "message": message,
            "segments": sms_segments(message),
            "to_phone": to_phone,
        })

    return request.app.state.templates.TemplateResponse(
        "partials/sms_preview.html",
        {"request": request, "previews": previews},
    )


@router.post("/api/sms/send")
def send_sms_bulk(
    request: Request,
    template: str = Form(...),
    lead_ids: str = Form(""),
    region: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not template.strip():
        return _error("Write a message first.")
    region = region.upper() if region.upper() in _REGIONS else ""

    leads = _user_leads(db, user.id, lead_ids)
    leads_with_phone = [l for l in leads if l.phone]
    if not leads_with_phone:
        return _error("No leads with phone numbers selected.")

    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "sms_send", len(leads_with_phone))
    if not has:
        return _error(f"Insufficient credits. Need {cost}, have {balance}.")

    keys = db.query(UserAPIKeys).filter_by(user_id=user.id).first()
    account_sid = (keys.twilio_account_sid or "").strip() if keys else ""
    auth_token = decrypt(keys.twilio_auth_token) if keys and keys.twilio_auth_token else ""
    from_number = (keys.twilio_phone_number or "").strip() if keys else ""
    missing = [label for label, value in (
        ("Account SID", account_sid), ("Auth Token", auth_token), ("phone number", from_number),
    ) if not value]
    if missing:
        return _error(f"Twilio is not fully configured — missing {', '.join(missing)}. Add them in Settings → API keys.")

    results = []
    sent = 0
    for lead in leads_with_phone:
        message = render_merge_fields(template, lead_merge_fields(lead))
        to_phone = normalize_phone(lead.phone, region or infer_region(lead.phone, lead.address))
        if not to_phone:
            results.append({"lead": lead, "ok": False, "error": f"Could not parse phone number {lead.phone!r}"})
            continue

        result = send_sms(to_phone, message, account_sid, auth_token, from_number)
        if result["success"]:
            credit_manager.deduct_credits(db, user.id, "sms_send", 1, f"SMS to {to_phone}")
            lead.last_sms_at = datetime.now(timezone.utc)
            lead.sms_sent_count = (lead.sms_sent_count or 0) + 1
            sent += 1
            results.append({"lead": lead, "ok": True, "to_phone": to_phone})
        else:
            results.append({"lead": lead, "ok": False, "error": result.get("error", "Unknown error")})

    # sms_send costs 0 credits, so deduct_credits never commits — persist tracking here.
    db.commit()

    return request.app.state.templates.TemplateResponse(
        "partials/sms_result.html",
        {"request": request, "results": results, "sent": sent, "failed": len(results) - sent},
    )
