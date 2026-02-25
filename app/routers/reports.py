import logging

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import Lead
from app.services.client_report import generate_client_report, generate_internal_report, render_client_report_html
from app.services.pdf_report import generate_client_pdf, generate_internal_pdf

logger = logging.getLogger(__name__)
router = APIRouter(tags=["reports"])


def _get_lead(db: Session, lead_id: str, user_id: int) -> Lead:
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user_id).first()
    if not lead:
        return None
    return lead


def _lead_data(lead: Lead) -> dict:
    return {
        "name": lead.name,
        "website": lead.website,
        "score": lead.score or 0,
        "email": lead.email or "",
        "phone": lead.phone or "",
        "address": lead.address or "",
        "heuristic_score": lead.heuristic_score or 0,
        "ai_score": lead.ai_score or 0,
        "score_breakdown": lead.score_breakdown,
        "technographics": lead.technographics,
    }


@router.post("/api/leads/{lead_id}/report/client")
def client_report_json(lead_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    lead = _get_lead(db, lead_id, user.id)
    if not lead:
        return JSONResponse({"error": "Lead not found"}, status_code=404)

    report = generate_client_report(_lead_data(lead))
    return JSONResponse(report)


@router.post("/api/leads/{lead_id}/report/client/html")
def client_report_html(lead_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    lead = _get_lead(db, lead_id, user.id)
    if not lead:
        return HTMLResponse("<p>Lead not found</p>", status_code=404)

    report = generate_client_report(_lead_data(lead))
    html = render_client_report_html(report)

    return request.app.state.templates.TemplateResponse(
        "partials/client_report.html",
        {"request": request, "report_html": html, "lead": lead},
    )


@router.post("/api/leads/{lead_id}/report/internal")
def internal_report(lead_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    lead = _get_lead(db, lead_id, user.id)
    if not lead:
        return JSONResponse({"error": "Lead not found"}, status_code=404)

    report = generate_internal_report(_lead_data(lead))
    return JSONResponse(report)


@router.post("/api/leads/{lead_id}/report/pdf")
def download_pdf(
    lead_id: str,
    request: Request,
    report_type: str = Form("client"),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    lead = _get_lead(db, lead_id, user.id)
    if not lead:
        return HTMLResponse("<p>Lead not found</p>", status_code=404)

    data = _lead_data(lead)

    if report_type == "internal":
        report = generate_internal_report(data)
        pdf_bytes = generate_internal_pdf(report)
        filename = f"{lead.name}_internal_report.pdf"
    else:
        report = generate_client_report(data)
        pdf_bytes = generate_client_pdf(report)
        filename = f"{lead.name}_audit_report.pdf"

    import io
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/leads/{lead_id}/report/email")
def email_report(
    lead_id: str,
    request: Request,
    subject: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    lead = _get_lead(db, lead_id, user.id)
    if not lead or not lead.email:
        return HTMLResponse('<div class="error-msg">Lead not found or has no email</div>')

    data = _lead_data(lead)
    report = generate_client_report(data)
    pdf_bytes = generate_client_pdf(report)

    from app.services.email_senders import send_email_with_attachment_for_user, EmailProviderError
    email_subject = subject or f"Website Audit Report — {lead.name}"
    email_body = body or f"<p>Hi,</p><p>Please find attached a website audit report for {lead.name}.</p><p>Best regards</p>"

    try:
        send_email_with_attachment_for_user(
            db, user.id, lead.email, email_subject, email_body,
            pdf_bytes, f"{lead.name}_audit.pdf",
        )
        return HTMLResponse('<span class="saved-flash">Report emailed</span>')
    except EmailProviderError as e:
        return HTMLResponse(f'<div class="error-msg">{e}</div>')
