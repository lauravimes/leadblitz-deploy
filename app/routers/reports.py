import html
import io
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import EmailSignature, Lead
from app.services.client_report import (
    ClientReportError,
    agency_branding,
    brand_report,
    cached_client_report,
    generate_client_report,
    generate_internal_report,
    lead_report_data,
    render_client_report_html,
    report_filename,
    store_client_report,
)
from app.services.pdf_report import generate_client_pdf, generate_internal_pdf

logger = logging.getLogger(__name__)
router = APIRouter(tags=["reports"])


def _get_lead(db: Session, lead_id: str, user_id: int) -> Optional[Lead]:
    return db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user_id).first()


def _branding(db: Session, user_id: int) -> Dict[str, str]:
    return agency_branding(db.query(EmailSignature).filter_by(user_id=user_id).first())


def client_report_for(db: Session, lead: Lead, agency: Dict[str, str]) -> Dict[str, Any]:
    """Cached report when still valid, otherwise generate, cache and commit.
    Raises ``ClientReportError`` — never returns an empty report."""
    report = cached_client_report(lead)
    if report is None:
        data = lead_report_data(lead)
        lead_id = lead.id
        # Release the connection while the LLM call runs, then re-attach.
        db.rollback()
        report = generate_client_report(data)
        lead = db.get(Lead, lead_id)
        if lead:
            store_client_report(lead, report)
            db.commit()
    return brand_report(report, agency)


@router.post("/api/leads/{lead_id}/report/client/html")
def client_report_html(lead_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    lead = _get_lead(db, lead_id, user.id)
    if not lead:
        return HTMLResponse('<div class="error-msg">Lead not found</div>')

    try:
        report = client_report_for(db, lead, _branding(db, user.id))
    except ClientReportError as exc:
        return HTMLResponse(f'<div class="error-msg">{html.escape(str(exc))}</div>')

    return request.app.state.templates.TemplateResponse(
        "partials/client_report.html",
        {"request": request, "report_html": render_client_report_html(report), "lead": lead},
    )


@router.post("/api/leads/{lead_id}/report/pdf")
def download_pdf(
    lead_id: str,
    request: Request,
    report_type: str = Form("client"),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    lead = _get_lead(db, lead_id, user.id)
    templates = request.app.state.templates
    if not lead:
        return templates.TemplateResponse(
            "partials/error.html", {"request": request, "message": "Lead not found"}, status_code=404,
        )

    if report_type == "internal":
        pdf_bytes = generate_internal_pdf(generate_internal_report(lead_report_data(lead)))
        filename = report_filename(lead.name, kind="internal-report")
    else:
        try:
            report = client_report_for(db, lead, _branding(db, user.id))
        except ClientReportError as exc:
            return templates.TemplateResponse(
                "partials/error.html", {"request": request, "message": str(exc)}, status_code=502,
            )
        pdf_bytes = generate_client_pdf(report)
        filename = report_filename(lead.name)

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
