"""Shared lead-list filtering so the leads page, "Email all filtered", batch
scoring and batch enrichment all agree on what "the current filter" means."""

from typing import Optional

from sqlalchemy.orm import Query

from app.models import Lead


def apply_lead_filters(
    q: Query,
    *,
    stage: Optional[str] = None,
    campaign_id: Optional[str] = None,
    import_id: Optional[str] = None,
    scored: Optional[str] = None,
    has_email: Optional[str] = None,
    search: Optional[str] = None,
) -> Query:
    if stage:
        q = q.filter(Lead.stage == stage)
    if campaign_id:
        q = q.filter(Lead.campaign_id == campaign_id)
    if import_id:
        q = q.filter(Lead.import_id == import_id)
    if scored == "1":
        q = q.filter(Lead.score.isnot(None))
    elif scored == "0":
        q = q.filter(Lead.score.is_(None))
    if has_email == "1":
        q = q.filter(Lead.email.isnot(None), Lead.email != "")
    elif has_email == "0":
        q = q.filter((Lead.email.is_(None)) | (Lead.email == ""))
    if search:
        pattern = f"%{search.strip()}%"
        q = q.filter(
            Lead.name.ilike(pattern)
            | Lead.email.ilike(pattern)
            | Lead.phone.ilike(pattern)
            | Lead.address.ilike(pattern)
            | Lead.website.ilike(pattern)
        )
    return q
