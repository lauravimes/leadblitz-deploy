"""DB-backed email send jobs.

``POST /api/email/send`` writes a ``SendJob`` plus one ``SendJobItem`` per lead
and returns immediately. One background worker thread (``start_worker``) polls
for due items, sends them, and updates counters. All state lives in Postgres,
so a deploy or a Render spin-down loses nothing: whatever is still ``queued``
is picked up when the process comes back.

Session discipline: each step opens its own short-lived ``SessionLocal()``.
The SMTP / OpenAI network calls happen with **no session open**.
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import EmailSignature, Lead, SendJob, SendJobItem
from app.services.client_report import (
    ClientReportError,
    brand_report,
    agency_branding,
    cached_client_report,
    generate_client_report,
    lead_report_data,
    report_filename,
    store_client_report,
)
from app.services.credits import credit_manager
from app.services.email_senders import (
    Attachment,
    EmailProviderError,
    deliver_email,
    get_email_settings,
    prepare_body,
)
from app.services.merge_fields import lead_merge_fields, render_merge_fields
from app.services.pdf_report import generate_client_pdf

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15
BATCH_SIZE = 10
MAX_ERROR_LEN = 2000

_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
_wake = threading.Event()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Job creation / inspection (called from request handlers) -------------------

def create_send_job(
    db: Session,
    user_id: int,
    leads: Sequence[Lead],
    subject: str,
    body: str,
    attach_report: bool = False,
    attachment: Optional[Attachment] = None,
    send_rate_per_day: int = 0,
) -> SendJob:
    """Persist a job and its items. Items for leads without an email address are
    created as ``skipped`` so the totals shown to the user are honest.

    ``send_rate_per_day`` > 0 spreads ``next_send_at`` evenly (100/day = one
    email every 864 s); 0 makes everything due immediately.
    """
    now = _utcnow()
    interval = timedelta(seconds=86400.0 / send_rate_per_day) if send_rate_per_day > 0 else timedelta(0)

    job = SendJob(
        user_id=user_id,
        subject=subject,
        body=body,
        attach_report=attach_report,
        send_rate_per_day=max(0, int(send_rate_per_day)),
        status="queued",
        total=len(leads),
    )
    if attachment:
        job.attachment_data, job.attachment_name, job.attachment_mime = attachment
    db.add(job)
    db.flush()

    slot = 0
    for lead in leads:
        if lead.email:
            db.add(SendJobItem(job_id=job.id, lead_id=lead.id, status="queued", next_send_at=now + interval * slot))
            slot += 1
        else:
            db.add(SendJobItem(job_id=job.id, lead_id=lead.id, status="skipped", error="No email address"))
    if slot == 0:
        job.status = "completed"
        job.completed_at = now
    db.commit()
    return job


def job_progress(db: Session, job: SendJob) -> Dict[str, Any]:
    """Counters for ``partials/send_progress.html``."""
    counts = dict(
        db.query(SendJobItem.status, func.count(SendJobItem.id))
        .filter(SendJobItem.job_id == job.id)
        .group_by(SendJobItem.status)
        .all()
    )
    sent = counts.get("sent", 0)
    failed = counts.get("failed", 0)
    skipped = counts.get("skipped", 0)
    remaining = counts.get("queued", 0) + counts.get("sending", 0)

    next_send_at = (
        db.query(func.min(SendJobItem.next_send_at))
        .filter(SendJobItem.job_id == job.id, SendJobItem.status == "queued")
        .scalar()
    )
    errors = [
        f"{name or 'Lead'}: {err}"
        for name, err in (
            db.query(Lead.name, SendJobItem.error)
            .join(Lead, Lead.id == SendJobItem.lead_id)
            .filter(SendJobItem.job_id == job.id, SendJobItem.status.in_(("failed", "skipped")), SendJobItem.error.isnot(None))
            .order_by(SendJobItem.id)
            .limit(25)
            .all()
        )
    ]
    return {
        "status": job.status,
        "total": job.total,
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "remaining": remaining,
        "send_rate": job.send_rate_per_day,
        "next_send_at": next_send_at,
        "errors": errors,
        "last_error": job.last_error,
    }


def cancel_job(db: Session, job: SendJob) -> None:
    """Skip everything not yet sent. Items currently ``sending`` finish."""
    db.query(SendJobItem).filter(SendJobItem.job_id == job.id, SendJobItem.status == "queued").update(
        {"status": "skipped", "error": "Cancelled"}, synchronize_session=False
    )
    job.status = "cancelled"
    job.completed_at = _utcnow()
    db.commit()


# --- Worker --------------------------------------------------------------------

def start_worker() -> Optional[threading.Thread]:
    """Start the single daemon worker thread (idempotent). Set
    ``SEND_JOBS_WORKER=0`` to disable, e.g. in tests that drive
    ``process_due_items`` directly."""
    global _worker
    if os.environ.get("SEND_JOBS_WORKER", "1") == "0":
        return None
    with _worker_lock:
        if _worker and _worker.is_alive():
            return _worker
        _worker = threading.Thread(target=_run_forever, name="send-jobs-worker", daemon=True)
        _worker.start()
        return _worker


def notify_worker() -> None:
    """Wake the worker so a 'send now' job starts within a second."""
    _wake.set()


def _run_forever() -> None:
    try:
        _recover_interrupted()
    except Exception:  # noqa: BLE001
        logger.exception("[send_jobs] recovery failed")
    while True:
        processed = 0
        try:
            processed = process_due_items()
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.exception("[send_jobs] worker iteration failed")
        _wake.wait(0.5 if processed >= BATCH_SIZE else POLL_INTERVAL_SECONDS)
        _wake.clear()


def _recover_interrupted() -> None:
    """Items left in ``sending`` belong to a process that died mid-send. We do
    not know whether the provider accepted them, so they are marked failed
    rather than re-sent (a duplicate cold email is worse than a visible gap)."""
    with SessionLocal() as db:
        stuck = db.query(SendJobItem).filter(SendJobItem.status == "sending").all()
        job_ids = set()
        for item in stuck:
            item.status = "failed"
            item.error = "Interrupted by a server restart while sending — not re-sent to avoid duplicates."
            job_ids.add(item.job_id)
        for job_id in job_ids:
            job = db.get(SendJob, job_id)
            if job:
                _sync_job(db, job)
        db.commit()
        if stuck:
            logger.warning("[send_jobs] marked %d interrupted item(s) failed", len(stuck))


def process_due_items(limit: int = BATCH_SIZE, now: Optional[datetime] = None) -> int:
    """Send up to ``limit`` due items. Returns how many were attempted."""
    now = now or _utcnow()
    with SessionLocal() as db:
        ids = [
            row[0]
            for row in db.query(SendJobItem.id)
            .filter(SendJobItem.status == "queued", SendJobItem.next_send_at <= now)
            .order_by(SendJobItem.next_send_at, SendJobItem.id)
            .limit(limit)
            .all()
        ]
    for item_id in ids:
        try:
            _send_one(item_id)
        except Exception:  # noqa: BLE001
            logger.exception("[send_jobs] item %s crashed", item_id)
            _record_result(item_id, ok=False, error="Unexpected internal error — see server logs.")
    return len(ids)


def _send_one(item_id: int) -> None:
    work = _claim(item_id)
    if not work:
        return

    report_to_cache: Optional[Dict[str, Any]] = None
    attachments: List[Attachment] = list(work["attachments"])
    error: Optional[str] = None
    try:
        if work["attach_report"] and work["lead_data"].get("score") is not None:
            report = work["cached_report"]
            if report is None:
                report = generate_client_report(work["lead_data"])  # OpenAI — no session held
                report_to_cache = report
            pdf = generate_client_pdf(brand_report(report, work["agency"]))
            attachments.append((pdf, report_filename(work["lead_data"].get("name")), "application/pdf"))
        deliver_email(work["settings"], work["to_email"], work["subject"], work["body"], attachments)
        ok = True
    except (EmailProviderError, ClientReportError) as exc:
        ok, error = False, str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[send_jobs] unexpected failure for item %s", item_id)
        ok, error = False, f"Unexpected error: {exc}"

    _record_result(item_id, ok=ok, error=error, report_to_cache=report_to_cache)


def _claim(item_id: int) -> Optional[Dict[str, Any]]:
    """Lock the item, charge credits, mark it ``sending`` and gather everything
    the send needs as plain values. Returns None when there is nothing to do."""
    with SessionLocal() as db:
        item = (
            db.query(SendJobItem)
            .filter(SendJobItem.id == item_id, SendJobItem.status == "queued")
            .with_for_update(skip_locked=True)
            .first()
        )
        if not item:
            return None
        job = db.get(SendJob, item.job_id)
        lead = db.get(Lead, item.lead_id)

        if not job or job.status == "cancelled":
            item.status, item.error = "skipped", "Cancelled"
            if job:
                _sync_job(db, job)
            db.commit()
            return None
        if not lead or not lead.email:
            item.status, item.error = "skipped", "Lead deleted or has no email address"
            _sync_job(db, job)
            db.commit()
            return None

        ok, balance = credit_manager.deduct_credits(db, job.user_id, "email_send", 1, f"Email to {lead.email}")
        if not ok:
            item.status, item.error = "failed", f"Insufficient credits ({balance} available)"
            _sync_job(db, job)
            db.commit()
            return None

        item.status = "sending"
        if job.status == "queued":
            job.status = "running"

        sig = db.query(EmailSignature).filter_by(user_id=job.user_id).first()
        settings = get_email_settings(db, job.user_id)
        fields = lead_merge_fields(lead)
        attachments: List[Attachment] = []
        if job.attachment_data and job.attachment_name:
            attachments.append((bytes(job.attachment_data), job.attachment_name, job.attachment_mime or "application/octet-stream"))

        work = {
            "to_email": lead.email,
            "subject": render_merge_fields(job.subject, fields),
            "body": prepare_body(render_merge_fields(job.body, fields), sig),
            "attach_report": bool(job.attach_report),
            "attachments": attachments,
            "lead_data": lead_report_data(lead),
            "cached_report": cached_client_report(lead),
            "agency": agency_branding(sig),
            "settings": settings,  # detached ORM row; attributes are loaded
        }
        db.commit()
        return work


def _record_result(item_id: int, ok: bool, error: Optional[str] = None,
                   report_to_cache: Optional[Dict[str, Any]] = None) -> None:
    with SessionLocal() as db:
        item = db.get(SendJobItem, item_id)
        if not item or item.status not in ("sending", "queued"):
            return
        job = db.get(SendJob, item.job_id)
        lead = db.get(Lead, item.lead_id)
        now = _utcnow()

        if ok:
            item.status, item.sent_at, item.error = "sent", now, None
            if lead:
                lead.last_emailed_at = now
                lead.emails_sent_count = (lead.emails_sent_count or 0) + 1
        else:
            item.status = "failed"
            item.error = (error or "Unknown error")[:MAX_ERROR_LEN]
            if job:
                job.last_error = item.error
                credit_manager.refund_credits(db, job.user_id, "email_send", 1, f"Refund: email to lead {item.lead_id} failed")

        if report_to_cache and lead:
            store_client_report(lead, report_to_cache)

        if job:
            _sync_job(db, job)
        db.commit()


def _sync_job(db: Session, job: SendJob) -> None:
    """Recompute counters from the items (single source of truth) and close the
    job when nothing is left to send."""
    db.flush()  # SessionLocal is autoflush=False; the count must see pending item updates
    counts = dict(
        db.query(SendJobItem.status, func.count(SendJobItem.id))
        .filter(SendJobItem.job_id == job.id)
        .group_by(SendJobItem.status)
        .all()
    )
    job.sent = counts.get("sent", 0)
    job.failed = counts.get("failed", 0)
    outstanding = counts.get("queued", 0) + counts.get("sending", 0)
    if outstanding == 0 and job.status not in ("completed", "cancelled"):
        job.status = "completed"
        job.completed_at = _utcnow()
