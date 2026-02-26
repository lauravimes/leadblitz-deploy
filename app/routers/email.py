import logging

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import User, Lead, EmailSignature, EmailTemplate
from app.services.credits import credit_manager
from app.services.email_senders import send_email_for_user, EmailProviderError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["email"])


@router.post("/api/email/preview")
def preview_emails(
    request: Request,
    subject: str = Form(""),
    body: str = Form(""),
    lead_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()][:5]
    leads = db.query(Lead).filter(Lead.id.in_(ids), Lead.user_id == user.id).all() if ids else []

    previews = []
    for lead in leads:
        rendered_subject = subject.replace("{{business_name}}", lead.name or "")
        rendered_body = body.replace("{{business_name}}", lead.name or "").replace("{{website}}", lead.website or "")
        previews.append({"lead": lead, "subject": rendered_subject, "body": rendered_body})

    return templates.TemplateResponse(
        "partials/email_preview.html",
        {"request": request, "previews": previews},
    )


@router.post("/api/email/send")
def send_emails(
    request: Request,
    subject: str = Form(...),
    body: str = Form(...),
    lead_ids: str = Form(""),
    attach_report: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()]
    leads = db.query(Lead).filter(Lead.id.in_(ids), Lead.user_id == user.id).all() if ids else []

    leads_with_email = [l for l in leads if l.email]
    if not leads_with_email:
        return HTMLResponse('<div class="error-msg">No leads with email addresses selected</div>')

    # Check credits
    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "email_send", len(leads_with_email))
    if not has:
        return HTMLResponse(f'<div class="error-msg">Insufficient credits. Need {cost}, have {balance}</div>')

    sent = 0
    errors = []
    for lead in leads_with_email:
        rendered_subject = subject.replace("{{business_name}}", lead.name or "")
        rendered_body = body.replace("{{business_name}}", lead.name or "").replace("{{website}}", lead.website or "")
        try:
            if attach_report == "1" and lead.score is not None:
                from app.services.client_report import generate_client_report
                from app.services.pdf_report import generate_client_pdf
                from app.services.email_senders import send_email_with_attachment_for_user
                lead_data = {
                    "name": lead.name, "website": lead.website,
                    "score": lead.score or 0, "email": lead.email or "",
                    "phone": lead.phone or "", "address": lead.address or "",
                    "heuristic_score": lead.heuristic_score or 0,
                    "ai_score": lead.ai_score or 0,
                    "score_breakdown": lead.score_breakdown,
                    "technographics": lead.technographics,
                }
                report = generate_client_report(lead_data)
                pdf_bytes = generate_client_pdf(report)
                filename = f"audit-report-{(lead.name or 'report').replace(' ', '-').lower()}.pdf"
                send_email_with_attachment_for_user(
                    db, user.id, lead.email, rendered_subject, rendered_body,
                    attachment_bytes=pdf_bytes, attachment_filename=filename,
                )
            else:
                send_email_for_user(db, user.id, lead.email, rendered_subject, rendered_body)
            credit_manager.deduct_credits(db, user.id, "email_send", 1, f"Email to {lead.email}")
            sent += 1
        except EmailProviderError as e:
            errors.append(f"{lead.name}: {e}")
        except Exception as e:
            logger.error(f"Email send error for {lead.name}: {e}")
            errors.append(f"{lead.name}: {e}")

    msg = f"Sent {sent} email{'s' if sent != 1 else ''}"
    if attach_report == "1":
        msg += " with report attached"
    if errors:
        msg += f". {len(errors)} failed."
    return HTMLResponse(f'<span class="saved-flash">{msg}</span>')


@router.post("/api/email/send-single")
def send_single_email(
    request: Request,
    lead_id: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
    if not lead or not lead.email:
        return HTMLResponse('<div class="error-msg">Lead not found or has no email</div>')

    try:
        send_email_for_user(db, user.id, lead.email, subject, body)
        credit_manager.deduct_credits(db, user.id, "email_send", 1, f"Email to {lead.email}")
        return HTMLResponse('<span class="saved-flash">Email sent</span>')
    except EmailProviderError as e:
        return HTMLResponse(f'<div class="error-msg">{e}</div>')


@router.post("/api/email/personalize")
def personalize_email(
    request: Request,
    lead_id: str = Form(...),
    base_pitch: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
    if not lead:
        return JSONResponse({"error": "Lead not found"}, status_code=404)

    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "email_personalization")
    if not has:
        return JSONResponse({"error": f"Insufficient credits. Need {cost}, have {balance}"}, status_code=400)

    # Get user's base pitch from signature if not provided
    if not base_pitch:
        sig = db.query(EmailSignature).filter_by(user_id=user.id).first()
        base_pitch = sig.base_pitch if sig else ""

    if not base_pitch:
        return JSONResponse({"error": "Please set a base pitch in your email signature settings"}, status_code=400)

    from app.services.ai_email import generate_personalized_email
    result = generate_personalized_email(
        {"name": lead.name, "website": lead.website, "score": lead.score},
        base_pitch,
    )
    credit_manager.deduct_credits(db, user.id, "email_personalization", 1, f"AI email for {lead.name}")
    return JSONResponse(result)


# --- Signatures ---

@router.get("/api/email/signatures")
def get_signature(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    sig = db.query(EmailSignature).filter_by(user_id=user.id).first()
    if not sig:
        return JSONResponse({"full_name": "", "position": "", "company_name": "", "phone": "", "website": "", "base_pitch": ""})
    return JSONResponse({
        "full_name": sig.full_name or "",
        "position": sig.position or "",
        "company_name": sig.company_name or "",
        "phone": sig.phone or "",
        "website": sig.website or "",
        "base_pitch": sig.base_pitch or "",
        "use_custom": sig.use_custom,
        "custom_signature": sig.custom_signature or "",
    })


@router.post("/api/email/signatures")
def save_signature(
    request: Request,
    full_name: str = Form(""),
    position: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    website: str = Form(""),
    base_pitch: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    sig = db.query(EmailSignature).filter_by(user_id=user.id).first()
    if not sig:
        sig = EmailSignature(user_id=user.id)
        db.add(sig)
    sig.full_name = full_name
    sig.position = position
    sig.company_name = company_name
    sig.phone = phone
    sig.website = website
    sig.base_pitch = base_pitch
    db.commit()
    return HTMLResponse('<span class="saved-flash">Signature saved</span>')


# --- Templates ---

@router.get("/api/email/templates")
def list_templates(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    templates_list = db.query(EmailTemplate).filter_by(user_id=user.id).order_by(EmailTemplate.created_at.desc()).all()
    return JSONResponse([
        {"id": t.id, "name": t.name, "subject": t.subject, "body": t.body}
        for t in templates_list
    ])


@router.post("/api/email/templates")
def save_template(
    request: Request,
    name: str = Form(...),
    subject: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    tpl = EmailTemplate(user_id=user.id, name=name, subject=subject, body=body)
    db.add(tpl)
    db.commit()
    return HTMLResponse('<span class="saved-flash">Template saved</span>')


@router.delete("/api/email/templates/{template_id}")
def delete_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    tpl = db.query(EmailTemplate).filter_by(id=template_id, user_id=user.id).first()
    if tpl:
        db.delete(tpl)
        db.commit()
    return HTMLResponse('<span class="saved-flash">Deleted</span>')
