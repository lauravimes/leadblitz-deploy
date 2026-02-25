import csv
import io
import logging

from fastapi import APIRouter, Request, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import Lead
from app.services.csv_import import (
    generate_import_id,
    get_csv_template,
    parse_csv_file,
    process_csv_rows,
    get_import_status,
    score_import_leads_background,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["csv"])


@router.get("/api/csv/template")
def download_template(request: Request):
    content = get_csv_template()
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leadblitz_import_template.csv"},
    )


@router.post("/api/csv/import")
async def import_csv(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = get_current_user(request, db)

    content = await file.read()
    rows, error = parse_csv_file(content, file.filename)

    if error:
        return HTMLResponse(f'<div class="error-msg">{error["message"]}</div>')

    import_id = generate_import_id()
    result = process_csv_rows(db, rows, user.id, import_id, file.filename)

    if not result["success"]:
        return HTMLResponse('<div class="error-msg">Import failed</div>')

    # Start background scoring
    lead_ids = result.get("_lead_ids_to_score", [])
    if lead_ids:
        score_import_leads_background(lead_ids, import_id, user.id)

    return templates.TemplateResponse(
        "partials/import_progress.html",
        {
            "request": request,
            "import_id": import_id,
            "message": result["message"],
            "summary": result["summary"],
        },
    )


@router.get("/api/csv/import/{import_id}/status")
def import_status(import_id: str, request: Request, db: Session = Depends(get_db)):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    status = get_import_status(db, import_id, user.id)

    if not status:
        return HTMLResponse('<div class="error-msg">Import not found</div>')

    return templates.TemplateResponse(
        "partials/import_progress.html",
        {"request": request, "import_id": import_id, "status": status},
    )


@router.get("/api/csv/export")
def export_csv(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    leads = db.query(Lead).filter(Lead.user_id == user.id).order_by(Lead.created_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "website", "email", "phone", "address", "score", "stage", "source", "notes"])
    for lead in leads:
        writer.writerow([
            lead.name, lead.website, lead.email or "", lead.phone,
            lead.address, lead.score or "", lead.stage, lead.source or "search", lead.notes or "",
        ])

    content = output.getvalue()
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leadblitz_leads_export.csv"},
    )
