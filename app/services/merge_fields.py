"""Merge fields shared by email and SMS outreach.

Both channels accept the same ``{{business_name}}`` / ``{{website}}`` /
``{{city}}`` / ``{{score}}`` placeholders, so the substitution logic lives here
once instead of drifting between the two routers.
"""
import re
from typing import Any, Dict, Optional

# "RG4 8US", "SW1A 1AA", "M1 1AE" — anywhere in a token
_UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE)
# "62701" or "62701-1234"
_US_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_US_STATE = re.compile(r"^[A-Z]{2}$")
_COUNTRIES = {
    "uk", "united kingdom", "great britain", "england", "scotland", "wales",
    "northern ireland", "usa", "us", "united states", "united states of america",
    "canada", "australia", "ireland",
}

MERGE_FIELDS = ("business_name", "website", "city", "score")


def city_from_address(address: Optional[str], campaign_location: Optional[str] = None) -> str:
    """Best-effort town/city from a Google Places formatted address.

    UK: ``"12 High St, Reading RG4 8US, UK"`` -> ``"Reading"``
    US: ``"123 Main St, Springfield, IL 62701, USA"`` -> ``"Springfield"``
    Falls back to the campaign's search location, then ``"your area"``.
    """
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if parts and parts[-1].lower() in _COUNTRIES:
        parts = parts[:-1]

    # Walk backwards past postcode / "STATE ZIP" tokens to the first token that
    # still has a name in it.
    for token in reversed(parts):
        cleaned = _UK_POSTCODE.sub("", token)
        cleaned = _US_ZIP.sub("", cleaned).strip(" ,")
        if not cleaned or _US_STATE.match(cleaned):
            continue
        # A street line ("12 High St") starts with a house number — not a city.
        if re.match(r"^\d", cleaned):
            continue
        return cleaned

    if campaign_location:
        return campaign_location.split(",")[0].strip() or "your area"
    return "your area"


def lead_merge_fields(lead: Any, campaign_location: Optional[str] = None) -> Dict[str, str]:
    """Build the substitution map for a Lead ORM object or a plain dict."""
    get = lead.get if isinstance(lead, dict) else (lambda k, d=None: getattr(lead, k, d))
    if campaign_location is None and not isinstance(lead, dict):
        campaign = getattr(lead, "campaign", None)
        campaign_location = getattr(campaign, "location", None) if campaign else None
    score = get("score")
    return {
        "business_name": get("name") or "",
        "name": get("name") or "",
        "website": get("website") or "",
        "city": city_from_address(get("address") or "", campaign_location),
        "score": "" if score is None else str(score),
        "phone": get("phone") or "",
    }


def render_merge_fields(text: str, fields: Dict[str, str]) -> str:
    """Replace ``{{key}}`` (optional inner whitespace) with the field value."""
    if not text:
        return ""

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        return fields.get(key, match.group(0))

    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", _sub, text)


def render_for_lead(text: str, lead: Any, campaign_location: Optional[str] = None) -> str:
    return render_merge_fields(text, lead_merge_fields(lead, campaign_location))
