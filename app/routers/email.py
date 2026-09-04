import html
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import EmailSignature, EmailTemplate, Lead, SendJob
from app.services.credits import credit_manager
from app.services.email_senders import signature_html
from app.services.merge_fields import lead_merge_fields, render_merge_fields
from app.services.send_jobs import cancel_job, create_send_job, job_progress, notify_worker

logger = logging.getLogger(__name__)
router = APIRouter(tags=["email"])

_MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB
_ALLOWED_RATES = {0, 100, 500}


def _error(message: str, status_code: int = 200) -> HTMLResponse:
    """HTMX 2 does not swap 4xx/5xx bodies, so user-facing errors go back as 200."""
    return HTMLResponse(f'<div class="error-msg">{html.escape(message)}</div>', status_code=status_code)


def _user_leads(db: Session, user_id: int, lead_ids: str, limit: Optional[int] = None) -> list[Lead]:
    ids = [lid.strip() for lid in lead_ids.split(",") if lid.strip()]
    if limit:
        ids = ids[:limit]
    if not ids:
        return []
    return db.query(Lead).filter(Lead.id.in_(ids), Lead.user_id == user_id).all()


# --- Compose ---------------------------------------------------------------------

@router.post("/api/email/preview")
def preview_emails(
    request: Request,
    subject: str = Form(""),
    body: str = Form(""),
    lead_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    leads = _user_leads(db, user.id, lead_ids, limit=5)
    sig = db.query(EmailSignature).filter_by(user_id=user.id).first()

    previews = []
    for lead in leads:
        fields = lead_merge_fields(lead)
        previews.append({
            "lead": lead,
            "subject": render_merge_fields(subject, fields),
            "body": render_merge_fields(body, fields),
        })

    return request.app.state.templates.TemplateResponse(
        "partials/email_preview.html",
        {"request": request, "previews": previews, "signature_html": signature_html(sig)},
    )


@router.post("/api/email/send")
def send_emails(
    request: Request,
    subject: str = Form(...),
    body: str = Form(...),
    lead_ids: str = Form(""),
    attach_report: str = Form(""),
    send_rate: int = Form(0),
    attachment: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """Create a SendJob; the background worker does the actual sending.

    Deliberately a plain ``def``: FastAPI runs it in the threadpool, so reading
    the upload and the DB writes never block the event loop.
    """
    user = get_current_user(request, db)
    if not subject.strip() or not body.strip():
        return _error("Subject and body are required.")

    leads = _user_leads(db, user.id, lead_ids)
    leads_with_email = [l for l in leads if l.email]
    if not leads_with_email:
        return _error("No leads with email addresses selected.")

    has, balance, cost = credit_manager.has_sufficient_credits(db, user.id, "email_send", len(leads_with_email))
    if not has:
        return _error(f"Insufficient credits. Need {cost}, have {balance}.")

    custom_attachment = None
    if attachment and attachment.filename:
        file_bytes = attachment.file.read()
        if len(file_bytes) > _MAX_ATTACHMENT_SIZE:
            return _error("Attachment too large (max 10 MB).")
        custom_attachment = (file_bytes, attachment.filename, attachment.content_type or "application/octet-stream")

    if send_rate not in _ALLOWED_RATES:
        send_rate = 0

    job = create_send_job(
        db, user.id, leads, subject, body,
        attach_report=(attach_report == "1"),
        attachment=custom_attachment,
        send_rate_per_day=send_rate,
    )
    notify_worker()

    return request.app.state.templates.TemplateResponse(
        "partials/send_progress.html",
        {"request": request, "job": job, "status": job_progress(db, job)},
    )


def _load_job(db: Session, job_id: str, user_id: int) -> Optional[SendJob]:
    return db.query(SendJob).filter(SendJob.id == job_id, SendJob.user_id == user_id).first()


@router.get("/api/email/send/{job_id}/status")
def send_status(job_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    job = _load_job(db, job_id, user.id)
    if not job:
        return HTMLResponse('<span class="subtext">Send job not found.</span>')
    return request.app.state.templates.TemplateResponse(
        "partials/send_progress.html",
        {"request": request, "job": job, "status": job_progress(db, job)},
    )


@router.post("/api/email/send/{job_id}/cancel")
def send_cancel(job_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    job = _load_job(db, job_id, user.id)
    if not job:
        return HTMLResponse('<span class="subtext">Send job not found.</span>')
    if job.status in ("queued", "running"):
        cancel_job(db, job)
    return request.app.state.templates.TemplateResponse(
        "partials/send_progress.html",
        {"request": request, "job": job, "status": job_progress(db, job)},
    )


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

    if not base_pitch:
        sig = db.query(EmailSignature).filter_by(user_id=user.id).first()
        base_pitch = sig.base_pitch if sig else ""
    if not base_pitch:
        return JSONResponse({"error": "Add a base pitch to your signature first (below), then try again."}, status_code=400)

    from app.services.ai_email import generate_personalized_email

    try:
        result = generate_personalized_email(
            {"name": lead.name, "website": lead.website, "score": lead.score},
            base_pitch,
        )
    except Exception as exc:  # noqa: BLE001 — provider errors become a message, not a 500
        logger.warning("[email] AI personalise failed for lead %s: %s", lead.id, exc)
        return JSONResponse({"error": f"AI writing failed: {exc}"}, status_code=502)

    credit_manager.deduct_credits(db, user.id, "email_personalization", 1, f"AI email for {lead.name}")
    return JSONResponse(result, headers={"HX-Trigger": "creditsChanged"})


# --- Signature ---------------------------------------------------------------------

@router.get("/api/email/signature-form")
def signature_form(request: Request, db: Session = Depends(get_db)):
    """Pre-filled signature form (the /email page pulls this in with hx-get)."""
    user = get_current_user(request, db)
    sig = db.query(EmailSignature).filter_by(user_id=user.id).first()
    return request.app.state.templates.TemplateResponse(
        "partials/signature_form.html",
        {"request": request, "sig": sig, "saved": False},
    )


@router.post("/api/email/signatures")
def save_signature(
    request: Request,
    full_name: Optional[str] = Form(None),
    position: Optional[str] = Form(None),
    company_name: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    website: Optional[str] = Form(None),
    base_pitch: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Update only the fields that were actually submitted."""
    user = get_current_user(request, db)
    sig = db.query(EmailSignature).filter_by(user_id=user.id).first()
    if not sig:
        sig = EmailSignature(user_id=user.id)
        db.add(sig)
    for field, value in (
        ("full_name", full_name), ("position", position), ("company_name", company_name),
        ("phone", phone), ("website", website), ("base_pitch", base_pitch),
    ):
        if value is not None:
            setattr(sig, field, value.strip())
    db.commit()
    return request.app.state.templates.TemplateResponse(
        "partials/signature_form.html",
        {"request": request, "sig": sig, "saved": True},
    )


# --- Templates -------------------------------------------------------------------

def _template_list(request: Request, db: Session, user_id: int, flash: str = ""):
    items = db.query(EmailTemplate).filter_by(user_id=user_id).order_by(EmailTemplate.created_at.desc()).all()
    return request.app.state.templates.TemplateResponse(
        "partials/template_list.html",
        {"request": request, "templates": items, "flash": flash},
    )


@router.get("/api/email/templates")
def list_templates(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return _template_list(request, db, user.id)


@router.post("/api/email/templates")
def save_template(
    request: Request,
    name: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    name = name.strip()
    if not name:
        return _template_list(request, db, user.id, flash="Give the template a name.")
    if not subject.strip() and not body.strip():
        return _template_list(request, db, user.id, flash="Write a subject or body first, then save it.")
    db.add(EmailTemplate(user_id=user.id, name=name[:255], subject=subject, body=body))
    db.commit()
    return _template_list(request, db, user.id, flash="Template saved.")


@router.delete("/api/email/templates/{template_id}")
def delete_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    tpl = db.query(EmailTemplate).filter_by(id=template_id, user_id=user.id).first()
    if tpl:
        db.delete(tpl)
        db.commit()
    return _template_list(request, db, user.id, flash="Template deleted.")
