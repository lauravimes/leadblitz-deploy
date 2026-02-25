import csv
import io
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Lead, CsvImport

logger = logging.getLogger(__name__)

TEMPLATE_HEADERS = ["business_name", "website_url", "email", "phone", "notes"]
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_ROWS = 1000


def generate_import_id() -> str:
    return f"imp_{uuid.uuid4().hex[:12]}"


def normalize_domain(url: str) -> str:
    url = url.strip().lower()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split("/")[0]
        domain = domain.lstrip("www.")
        return domain.rstrip("/")
    except Exception:
        return url.strip().lower()


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def validate_url_format(url: str) -> bool:
    url = url.strip()
    if not url:
        return False
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        return bool(parsed.netloc) and "." in parsed.netloc
    except Exception:
        return False


def get_csv_template() -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(["Joe's Plumbing", "https://joesplumbing.com", "joe@joesplumbing.com", "555-1234", "Referred by Mike"])
    return output.getvalue()


def parse_csv_file(file_content: bytes, filename: str) -> Tuple[Optional[List[Dict]], Optional[Dict]]:
    if len(file_content) > MAX_FILE_SIZE:
        return None, {"error": "too_large", "message": "Maximum 1000 leads per import."}

    try:
        text = file_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = file_content.decode("latin-1")
        except Exception:
            return None, {"error": "invalid_format", "message": "Invalid CSV file."}

    text = text.strip()
    if not text:
        return None, {"error": "empty_file", "message": "No data found in CSV"}

    try:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return None, {"error": "empty_file", "message": "No data found in CSV"}

        headers_lower = [h.strip().lower() for h in reader.fieldnames]
        if "website_url" not in headers_lower:
            return None, {"error": "no_url_column", "message": "Missing website_url column."}

        rows = []
        for row in reader:
            rows.append(row)
            if len(rows) > MAX_ROWS:
                return None, {"error": "too_large", "message": "Maximum 1000 leads per import."}

        if not rows:
            return None, {"error": "empty_file", "message": "No data found in CSV"}

        parsed = []
        for row in rows:
            normalized = {}
            for key, value in row.items():
                if key:
                    normalized[key.strip().lower()] = (value or "").strip()
            parsed.append(normalized)
        return parsed, None
    except csv.Error:
        return None, {"error": "invalid_format", "message": "Invalid CSV file."}


def process_csv_rows(db: Session, rows: List[Dict], user_id: int, import_id: str, filename: str) -> Dict:
    existing_leads = db.query(Lead).filter(Lead.user_id == user_id).all()
    existing_domains = {normalize_domain(l.website) for l in existing_leads if l.website}

    seen_domains = set()
    skipped_no_url = 0
    skipped_duplicate = 0
    skipped_invalid = 0
    valid_leads = []

    for row in rows:
        url = row.get("website_url", "").strip()
        if not url:
            skipped_no_url += 1
            continue
        if not validate_url_format(url):
            skipped_invalid += 1
            continue
        domain = normalize_domain(url)
        if domain in seen_domains or domain in existing_domains:
            skipped_duplicate += 1
            continue
        seen_domains.add(domain)
        valid_leads.append(row)

    to_score = len(valid_leads)

    csv_import = CsvImport(
        id=import_id,
        user_id=user_id,
        filename=filename,
        total_rows=len(rows),
        to_score=to_score,
        scored_count=0,
        unreachable_count=0,
        pending_count=to_score,
        status="in_progress",
        skipped_duplicate=skipped_duplicate,
        skipped_no_url=skipped_no_url,
        skipped_invalid=skipped_invalid,
    )
    db.add(csv_import)
    db.flush()

    lead_ids_to_score = []
    for row in valid_leads:
        lead_id = str(uuid.uuid4())
        url = normalize_url(row.get("website_url", ""))
        name = row.get("business_name", "").strip() or normalize_domain(url)
        email = row.get("email", "").strip()
        phone = row.get("phone", "").strip()
        notes = row.get("notes", "").strip()

        lead = Lead(
            id=lead_id,
            user_id=user_id,
            name=name,
            website=url,
            email=email or None,
            phone=phone,
            notes=notes,
            source="import",
            import_id=import_id,
            import_status="queued",
            stage="new",
        )
        db.add(lead)
        lead_ids_to_score.append(lead_id)

    db.commit()

    return {
        "success": True,
        "import_id": import_id,
        "summary": {
            "total_rows": len(rows),
            "to_score": to_score,
            "skipped_duplicate": skipped_duplicate,
            "skipped_no_url": skipped_no_url,
            "skipped_invalid": skipped_invalid,
        },
        "message": f"{to_score} leads imported.",
        "_lead_ids_to_score": lead_ids_to_score,
    }


def get_import_status(db: Session, import_id: str, user_id: int) -> Optional[Dict]:
    csv_import = db.query(CsvImport).filter_by(id=import_id, user_id=user_id).first()
    if not csv_import:
        return None

    leads = db.query(Lead).filter_by(import_id=import_id, user_id=user_id).all()
    scored = sum(1 for l in leads if l.import_status == "scored")
    unreachable = sum(1 for l in leads if l.import_status == "unreachable")
    pending = sum(1 for l in leads if l.import_status in ("queued", "scoring"))
    total = csv_import.to_score

    if pending == 0 and csv_import.status == "in_progress":
        csv_import.status = "completed"
        csv_import.completed_at = datetime.now(timezone.utc)
        csv_import.scored_count = scored
        csv_import.unreachable_count = unreachable
        csv_import.pending_count = 0
        db.commit()

    return {
        "import_id": import_id,
        "status": csv_import.status,
        "total": total,
        "scored": scored,
        "unreachable": unreachable,
        "pending": pending,
    }


def score_import_leads_background(lead_ids: List[str], import_id: str, user_id: int):
    thread = threading.Thread(
        target=_run_scoring_thread,
        args=(lead_ids, import_id, user_id),
        daemon=True,
    )
    thread.start()


def _run_scoring_thread(lead_ids: List[str], import_id: str, user_id: int):
    import concurrent.futures

    from app.config import get_settings
    from app.services.scorer import score_website_hybrid

    settings = get_settings()
    semaphore = threading.Semaphore(10)

    def score_single_lead(lead_id: str):
        with semaphore:
            db = SessionLocal()
            try:
                lead = db.query(Lead).filter_by(id=lead_id, user_id=user_id).first()
                if not lead or not lead.website:
                    return

                lead.import_status = "scoring"
                db.commit()

                try:
                    result = score_website_hybrid(db=db, url=lead.website, api_key=settings.openai_api_key)
                    lead.score = result.get("final_score", 0)
                    lead.heuristic_score = result.get("heuristic_score", 0)
                    lead.ai_score = result.get("ai_score", 0)
                    lead.score_breakdown = result
                    lead.technographics = result.get("technographics")
                    lead.last_scored_at = datetime.now(timezone.utc)
                    lead.import_status = "scored"
                    db.commit()
                except Exception as e:
                    logger.error(f"CSV scoring error for {lead.website}: {e}")
                    lead.import_status = "unreachable"
                    lead.score = 0
                    db.commit()
            finally:
                db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(score_single_lead, lid) for lid in lead_ids]
        concurrent.futures.wait(futures)

    db = SessionLocal()
    try:
        csv_import = db.query(CsvImport).filter_by(id=import_id).first()
        if csv_import:
            leads = db.query(Lead).filter_by(import_id=import_id).all()
            csv_import.scored_count = sum(1 for l in leads if l.import_status == "scored")
            csv_import.unreachable_count = sum(1 for l in leads if l.import_status == "unreachable")
            csv_import.pending_count = 0
            csv_import.status = "completed"
            csv_import.completed_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()

    logger.info(f"CSV import {import_id} scoring complete")
