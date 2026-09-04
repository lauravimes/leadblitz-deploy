import csv
import io
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Lead, CsvImport

logger = logging.getLogger(__name__)

TEMPLATE_HEADERS = ["business_name", "email", "phone", "website_url", "notes"]
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_ROWS = 10000
SCORING_WORKERS = 5

# Accept common header variants so exports from other tools import cleanly.
HEADER_ALIASES = {
    "business_name": {"business_name", "business", "name", "company", "company_name", "business name", "company name"},
    "email": {"email", "e-mail", "email_address", "email address", "contact_email"},
    "phone": {"phone", "telephone", "phone_number", "phone number", "mobile", "tel"},
    "website_url": {"website_url", "website", "url", "site", "web", "domain", "website url", "homepage"},
    "notes": {"notes", "note", "comments", "comment", "description"},
}


def generate_import_id() -> str:
    return f"imp_{uuid.uuid4().hex[:12]}"


def normalize_domain(url: str) -> str:
    url = url.strip().lower()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split("/")[0]
        domain = domain.removeprefix("www.")
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
    writer.writerow(["Joe's Plumbing", "joe@joesplumbing.com", "555-1234", "https://joesplumbing.com", "Referred by Mike"])
    writer.writerow(["Smith Conservatories", "info@smithconservatories.co.uk", "0118 123 4567", "", ""])
    return output.getvalue()


def _canonical_header(header: str) -> Optional[str]:
    h = (header or "").strip().lower().lstrip("﻿")
    for canonical, aliases in HEADER_ALIASES.items():
        if h in aliases:
            return canonical
    return None


def parse_csv_file(file_content: bytes, filename: str) -> Tuple[Optional[List[Dict]], Optional[Dict]]:
    if len(file_content) > MAX_FILE_SIZE:
        return None, {"error": "too_large", "message": "File is too large. Maximum size is 10 MB."}

    try:
        text = file_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_content.decode("latin-1", errors="replace")

    text = text.strip()
    if not text:
        return None, {"error": "empty_file", "message": "No data found in CSV"}

    try:
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            return None, {"error": "empty_file", "message": "No data found in CSV"}

        header_map = {}
        for raw in reader.fieldnames:
            canonical = _canonical_header(raw)
            if canonical and canonical not in header_map.values():
                header_map[raw] = canonical
        if not header_map:
            return None, {
                "error": "missing_columns",
                "message": "CSV must have at least one recognised column: business_name, email, website_url, phone, or notes.",
            }

        parsed = []
        for row in reader:
            normalized = {}
            for raw_key, value in row.items():
                canonical = header_map.get(raw_key)
                if canonical:
                    normalized[canonical] = (value or "").strip()
            if not any(normalized.values()):
                continue
            parsed.append(normalized)
            if len(parsed) > MAX_ROWS:
                return None, {"error": "too_large", "message": f"Maximum {MAX_ROWS:,} leads per import."}

        if not parsed:
            return None, {"error": "empty_file", "message": "No data found in CSV"}
        return parsed, None
    except csv.Error:
        return None, {"error": "invalid_format", "message": "Invalid CSV file."}


def process_csv_rows(db: Session, rows: List[Dict], user_id: int, import_id: str, filename: str) -> Dict:
    existing_websites = db.query(Lead.website).filter(Lead.user_id == user_id, Lead.website != "").all()
    existing_domains = {normalize_domain(w[0]) for w in existing_websites if w[0]}

    seen_domains = set()
    skipped_empty = 0
    skipped_duplicate = 0
    skipped_invalid = 0
    valid_leads = []

    for row in rows:
        name = row.get("business_name", "").strip()
        email = row.get("email", "").strip()
        url = row.get("website_url", "").strip()

        if not name and not email and not url:
            skipped_empty += 1
            continue

        if url:
            if not validate_url_format(url):
                skipped_invalid += 1
                continue
            domain = normalize_domain(url)
            if domain in seen_domains or domain in existing_domains:
                skipped_duplicate += 1
                continue
            seen_domains.add(domain)

        valid_leads.append(row)

    to_score = sum(1 for r in valid_leads if r.get("website_url", "").strip())

    csv_import = CsvImport(
        id=import_id,
        user_id=user_id,
        filename=(filename or "")[:500],
        total_rows=len(rows),
        to_score=to_score,
        scored_count=0,
        unreachable_count=0,
        pending_count=to_score,
        status="in_progress" if to_score else "completed",
        completed_at=None if to_score else datetime.now(timezone.utc),
        skipped_duplicate=skipped_duplicate,
        skipped_no_url=skipped_empty,
        skipped_invalid=skipped_invalid,
    )
    db.add(csv_import)
    db.flush()

    lead_ids_to_score = []
    for row in valid_leads:
        lead_id = str(uuid.uuid4())
        raw_url = row.get("website_url", "").strip()
        url = normalize_url(raw_url) if raw_url else ""
        name = row.get("business_name", "").strip()
        if not name and url:
            name = normalize_domain(url)
        elif not name:
            name = row.get("email", "").strip()
        email = row.get("email", "").strip()
        phone = row.get("phone", "").strip()
        notes = row.get("notes", "").strip()

        has_website = bool(raw_url)
        lead = Lead(
            id=lead_id,
            user_id=user_id,
            name=name[:500],
            website=url if has_website else "",
            email=(email[:255] or None),
            phone=phone[:50],
            notes=notes,
            source="import",
            import_id=import_id,
            import_status="queued" if has_website else None,
            stage="new",
        )
        db.add(lead)
        if has_website:
            lead_ids_to_score.append(lead_id)

    db.commit()

    imported = len(valid_leads)
    return {
        "success": True,
        "import_id": import_id,
        "summary": {
            "total_rows": len(rows),
            "imported": imported,
            "to_score": to_score,
            "skipped_duplicate": skipped_duplicate,
            "skipped_empty": skipped_empty,
            "skipped_invalid": skipped_invalid,
        },
        "message": f"{imported} lead{'s' if imported != 1 else ''} imported. {to_score} with a website will be scored (1 credit each).",
        "_lead_ids_to_score": lead_ids_to_score,
    }


def _status_counts(db: Session, import_id: str, user_id: int) -> Dict[str, int]:
    rows = (
        db.query(Lead.import_status, func.count(Lead.id))
        .filter(Lead.import_id == import_id, Lead.user_id == user_id)
        .group_by(Lead.import_status)
        .all()
    )
    return {(status or "none"): count for status, count in rows}


def get_import_status(db: Session, import_id: str, user_id: int) -> Optional[Dict]:
    csv_import = db.query(CsvImport).filter_by(id=import_id, user_id=user_id).first()
    if not csv_import:
        return None

    counts = _status_counts(db, import_id, user_id)
    scored = counts.get("scored", 0)
    unreachable = counts.get("unreachable", 0)
    pending_credits = counts.get("pending_credits", 0)
    pending = counts.get("queued", 0) + counts.get("scoring", 0)
    total = csv_import.to_score

    if pending == 0 and csv_import.status == "in_progress":
        csv_import.status = "partial" if pending_credits else "completed"
        csv_import.completed_at = datetime.now(timezone.utc)
        csv_import.scored_count = scored
        csv_import.unreachable_count = unreachable
        csv_import.pending_credits_count = pending_credits
        csv_import.pending_count = 0
        db.commit()

    return {
        "import_id": import_id,
        "status": csv_import.status,
        "total": total,
        "scored": scored,
        "unreachable": unreachable,
        "pending": pending,
        "pending_credits": pending_credits,
    }


def score_import_leads_background(lead_ids: List[str], import_id: str, user_id: int):
    if not lead_ids:
        return
    thread = threading.Thread(
        target=_run_scoring_thread,
        args=(lead_ids, import_id, user_id),
        daemon=True,
    )
    thread.start()


def resume_pending_imports() -> int:
    """At startup, restart scoring for imports interrupted by a deploy/restart."""
    db = SessionLocal()
    try:
        # Anything left mid-flight goes back to the queue
        db.query(Lead).filter(Lead.import_status == "scoring", Lead.import_id.isnot(None)).update(
            {"import_status": "queued"}, synchronize_session=False
        )
        db.commit()
        pending = (
            db.query(Lead.import_id, Lead.user_id, Lead.id)
            .filter(Lead.import_status == "queued", Lead.import_id.isnot(None))
            .all()
        )
    finally:
        db.close()

    grouped: Dict[tuple, List[str]] = {}
    for import_id, user_id, lead_id in pending:
        grouped.setdefault((import_id, user_id), []).append(lead_id)
    for (import_id, user_id), ids in grouped.items():
        logger.info("Resuming CSV import %s (%d leads)", import_id, len(ids))
        score_import_leads_background(ids, import_id, user_id)
    return len(pending)


def _run_scoring_thread(lead_ids: List[str], import_id: str, user_id: int):
    import concurrent.futures

    from app.config import get_settings
    from app.routers.scoring import score_lead_charged

    settings = get_settings()
    stop = threading.Event()

    def score_single_lead(lead_id: str):
        if stop.is_set():
            _mark(lead_id, "pending_credits")
            return
        db = SessionLocal()
        try:
            lead = db.query(Lead).filter_by(id=lead_id, user_id=user_id).first()
            if not lead or not lead.website:
                return
            if lead.import_status != "queued":
                return  # already handled (e.g. scored by a batch)

            lead.import_status = "scoring"
            db.commit()

            result = score_lead_charged(db, lead, user_id, settings.openai_api_key)
            if result.get("insufficient_credits"):
                stop.set()
                lead.import_status = "pending_credits"
                db.commit()
        except Exception as e:
            logger.error(f"CSV scoring error for lead {lead_id}: {e}")
            db.rollback()
            _mark(lead_id, "unreachable")
        finally:
            db.close()

    def _mark(lead_id: str, status: str):
        db = SessionLocal()
        try:
            db.query(Lead).filter_by(id=lead_id, user_id=user_id).update({"import_status": status})
            db.commit()
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=SCORING_WORKERS) as executor:
        futures = [executor.submit(score_single_lead, lid) for lid in lead_ids]
        concurrent.futures.wait(futures)

    db = SessionLocal()
    try:
        get_import_status(db, import_id, user_id)  # finalises the CsvImport row
    finally:
        db.close()

    logger.info(f"CSV import {import_id} scoring complete")
