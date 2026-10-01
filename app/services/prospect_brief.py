"""Separate observed facts, uncertain signals and human-checked outreach hooks."""
from datetime import datetime, timezone, timedelta

STAGES = ("new", "reviewing", "qualified", "contacted", "replied", "meeting", "proposal", "won", "lost", "rejected")
STAGE_LABELS = {"new": "New", "reviewing": "Reviewing", "qualified": "Qualified", "contacted": "Contacted", "replied": "Replied", "meeting": "Meeting booked", "proposal": "Proposal sent", "won": "Won", "lost": "Lost", "rejected": "Not a fit"}


def prospect_brief(lead):
    data = lead.score_breakdown or {}
    evidence = data.get("evidence") or {}
    uncertain = bool(data.get("rendering_limitations") or data.get("insufficient_evidence") or data.get("has_errors"))
    observations = []
    if not data.get("has_errors"):
        if evidence.get("title"):
            observations.append("Page title detected: " + str(evidence["title"])[:150])
        if evidence.get("https"):
            observations.append("The fetched page used HTTPS.")
        if evidence.get("viewport"):
            observations.append("A mobile viewport tag was detected; the rendered layout was not tested.")
        if evidence.get("cta_buttons"):
            observations.append("Links/buttons detected: " + ", ".join(str(x)[:60] for x in evidence["cta_buttons"][:3]))
    checks = []
    if lead.website:
        checks.append("Open the site on a phone and check the enquiry path before describing a fault.")
        if not evidence.get("viewport") and lead.score is not None:
            checks.append("No viewport tag was detected in the fetched HTML. Check the mobile layout yourself.")
        if evidence.get("ssl_invalid"):
            checks.append("The automated request encountered a certificate error. Verify it in your browser.")
    else:
        checks.append("The listing has no website URL. Check whether the business has a site elsewhere.")
    if uncertain:
        checks.append("This audit has incomplete evidence. Missing content may be a fetch limitation.")
    fit = []
    if lead.review_count:
        fit.append(f"{lead.review_count} Google reviews recorded; review recency and buying intent are unknown.")
    if lead.email or lead.phone:
        fit.append("A contact route is available; confirm it belongs to the business.")
    if not fit:
        fit.append("Check business activity and a usable contact route before investing more time.")
    verified_at = getattr(lead, "verified_issue_at", None)
    if verified_at and verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    fresh = bool(verified_at and verified_at >= datetime.now(timezone.utc) - timedelta(days=30))
    issue = (getattr(lead, "verified_issue", "") or "") if fresh else ""
    return {"observations": observations, "checks": checks, "fit": fit,
            "verified_issue": issue, "verification_stale": bool(getattr(lead, "verified_issue", None) and not fresh),
            "limited": uncertain, "source_url": lead.website or "", "scored_at": str(lead.last_scored_at or "")}
