import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from openai import OpenAI

from app.config import get_settings
from app.services.technographics import classify_tech_health

logger = logging.getLogger(__name__)


class ClientReportError(Exception):
    """The AI report could not be produced. Callers must not fall back to an
    empty report — surface the error instead."""


def slugify(value: str, fallback: str = "report") -> str:
    """Filesystem/header-safe slug for Content-Disposition filenames."""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:80] or fallback


def report_filename(lead_name: Optional[str], kind: str = "audit-report") -> str:
    return f"{kind}-{slugify(lead_name or '')}.pdf"


def lead_report_data(lead: Any) -> Dict[str, Any]:
    """Snapshot of the Lead fields the report generators need (plain values so
    it can be used after the DB session is closed)."""
    return {
        "id": lead.id,
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


def agency_branding(sig: Any) -> Dict[str, str]:
    """Branding block from the user's EmailSignature (empty strings when unset)."""
    if not sig:
        return {"agency_name": "", "agency_website": "", "agency_contact": ""}
    contact = " · ".join(x for x in (sig.full_name or "", sig.position or "") if x)
    return {
        "agency_name": sig.company_name or "",
        "agency_website": sig.website or "",
        "agency_contact": contact,
    }


def brand_report(report: Dict[str, Any], agency: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """Return a copy of ``report`` with the agency branding applied. Branding is
    applied at render time so a cached report follows signature changes."""
    branded = dict(report)
    branded.update(agency or agency_branding(None))
    return branded


def cached_client_report(lead: Any) -> Optional[Dict[str, Any]]:
    """The stored report if it is still valid (lead not re-scored since)."""
    report = lead.client_report
    if not report or not lead.client_report_at or "sections" not in report:
        return None
    scored_at = lead.last_scored_at
    if scored_at and scored_at > lead.client_report_at:
        return None
    return report


def store_client_report(lead: Any, report: Dict[str, Any]) -> None:
    """Cache the unbranded report on the lead (caller commits)."""
    lead.client_report = {k: v for k, v in report.items() if not k.startswith("agency_")}
    lead.client_report_at = datetime.now(timezone.utc)


def generate_client_report(
    lead_data: Dict[str, Any],
    agency: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Ask the LLM for a client-facing audit. Raises ``ClientReportError`` on any
    failure — never returns a partial/empty report."""
    technographics = lead_data.get("technographics") or {}
    score_breakdown = lead_data.get("score_breakdown") or {}
    score = lead_data.get("score", 0)
    business_name = lead_data.get("name", "Business")
    website = lead_data.get("website", "")

    if isinstance(score_breakdown, str):
        try:
            score_breakdown = json.loads(score_breakdown)
        except (ValueError, TypeError):
            score_breakdown = {}

    plain_report = score_breakdown.get("plain_english_report", {})
    tech_health = classify_tech_health(technographics) if technographics else {"green": [], "amber": [], "red": []}
    tech_summary = _build_tech_summary(technographics)

    s = get_settings()
    if not s.openai_api_key:
        raise ClientReportError("OpenAI API key is not configured.")
    client = OpenAI(api_key=s.openai_api_key, timeout=60.0)

    prompt = f"""Generate a professional website audit report for a business owner.

BUSINESS: {business_name}
WEBSITE: {website}
OVERALL SCORE: {score}/100

TECHNOLOGY FINDINGS:
{tech_summary}

STRENGTHS: {json.dumps(plain_report.get('strengths', []), indent=2)}
WEAKNESSES: {json.dumps(plain_report.get('weaknesses', []), indent=2)}
TECHNOLOGY OBSERVATIONS: {plain_report.get('technology_observations', 'Not available')}

GREEN (Good): {json.dumps([item['label'] + ' - ' + item['detail'] for item in tech_health.get('green', []) if isinstance(item, dict)])}
AMBER (Needs attention): {json.dumps([item['label'] + ' - ' + item['detail'] for item in tech_health.get('amber', []) if isinstance(item, dict)])}
RED (Critical): {json.dumps([item['label'] + ' - ' + item['detail'] for item in tech_health.get('red', []) if isinstance(item, dict)])}

Write the report in JSON format:
{{
    "executive_summary": "2-3 sentence overview",
    "overall_grade": "A/B/C/D/F (A=80-100, B=60-79, C=40-59, D=20-39, F=0-19)",
    "sections": [
        {{
            "title": "Section name",
            "status": "good/needs_attention/critical",
            "finding": "1-2 sentences",
            "impact": "Why this matters. 1-2 sentences.",
            "recommendation": "What to do. 1 sentence."
        }}
    ],
    "top_priorities": ["Top 3 actionable items"],
    "positive_highlights": ["2-3 positive things"]
}}

RULES: Write for a non-technical business owner. No jargon. Be professional and helpful. 5-8 sections."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a professional web consultant creating audit reports for business owners."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            response_format={"type": "json_object"},
        )

        report = json.loads(response.choices[0].message.content or "")
    except Exception as e:
        logger.error(f"[client_report] Failed for {business_name}: {e}")
        raise ClientReportError(f"Report generation failed: {e}") from e

    if not isinstance(report, dict) or not isinstance(report.get("sections"), list) or not report["sections"]:
        logger.error(f"[client_report] Malformed report for {business_name}: {str(report)[:200]}")
        raise ClientReportError("Report generation returned no findings — please try again.")

    report["business_name"] = business_name
    report["website"] = website
    report["score"] = score
    report["tech_health"] = tech_health
    report["technographics"] = technographics
    return brand_report(report, agency)


def generate_internal_report(lead_data: Dict[str, Any]) -> Dict[str, Any]:
    technographics = lead_data.get("technographics") or {}
    score_breakdown = lead_data.get("score_breakdown") or {}
    score = lead_data.get("score", 0)
    business_name = lead_data.get("name", "Business")
    website = lead_data.get("website", "")

    if isinstance(score_breakdown, str):
        try:
            score_breakdown = json.loads(score_breakdown)
        except (ValueError, TypeError):
            score_breakdown = {}

    plain_report = score_breakdown.get("plain_english_report", {})
    tech_health = classify_tech_health(technographics) if technographics else {"green": [], "amber": [], "red": []}
    hybrid = score_breakdown.get("hybrid_breakdown", {})

    return {
        "business_name": business_name,
        "website": website,
        "score": score,
        "email": lead_data.get("email", ""),
        "phone": lead_data.get("phone", ""),
        "address": lead_data.get("address", ""),
        "scoring": {
            "total": score,
            "heuristic": hybrid.get("heuristic_score", lead_data.get("heuristic_score", 0)),
            "ai": hybrid.get("ai_score", lead_data.get("ai_score", 0)),
            "confidence": score_breakdown.get("confidence", 0),
        },
        "report": plain_report,
        "technographics": technographics,
        "tech_health": tech_health,
    }


def render_client_report_html(report: Dict[str, Any]) -> str:
    """Standalone HTML document for the in-app preview iframe. Every value that
    came from the lead or the LLM is escaped."""
    e = lambda v: html.escape(str(v if v is not None else ""))  # noqa: E731

    business_name = e(report.get("business_name", "Business"))
    website = e(report.get("website", ""))
    score = e(report.get("score", 0))
    grade = e(report.get("overall_grade", "N/A"))
    executive_summary = e(report.get("executive_summary", ""))
    sections = report.get("sections", []) or []
    top_priorities = report.get("top_priorities", []) or []
    positive_highlights = report.get("positive_highlights", []) or []
    agency_name = e(report.get("agency_name", ""))
    agency_website = e(report.get("agency_website", ""))
    agency_contact = e(report.get("agency_contact", ""))

    grade_color = {"A": "#16a34a", "B": "#22c55e", "C": "#eab308", "D": "#f97316", "F": "#ef4444"}.get(grade, "#6b7280")

    sections_html = ""
    for section in sections:
        if not isinstance(section, dict):
            continue
        status = section.get("status", "needs_attention")
        status_color = {"good": "#16a34a", "needs_attention": "#eab308", "critical": "#ef4444"}.get(status, "#6b7280")
        status_label = {"good": "Good", "needs_attention": "Needs Attention", "critical": "Critical"}.get(status, "Unknown")
        sections_html += f"""
        <div style="border:1px solid #e5e7eb;border-radius:6px;padding:20px;margin-bottom:16px;border-left:4px solid {status_color}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <h3 style="margin:0;font-size:16px;color:#111">{e(section.get('title', ''))}</h3>
                <span style="color:{status_color};font-size:12px;font-weight:600">{status_label}</span>
            </div>
            <p style="margin:8px 0;color:#374151;font-size:14px"><strong>Finding:</strong> {e(section.get('finding', ''))}</p>
            <p style="margin:4px 0;color:#6b7280;font-size:13px"><strong>Impact:</strong> {e(section.get('impact', ''))}</p>
            <p style="margin:4px 0;color:#0066ff;font-size:13px;font-weight:500"><strong>Recommendation:</strong> {e(section.get('recommendation', ''))}</p>
        </div>"""

    highlights_html = "".join(
        f'<div style="margin-bottom:6px;color:#166534">&#10004; {e(h)}</div>' for h in positive_highlights
    )
    priorities_html = "".join(
        f'<div style="margin-bottom:8px"><span style="font-weight:700;color:#111">{i}.</span> {e(p)}</div>'
        for i, p in enumerate(top_priorities, 1)
    )
    agency_line = " · ".join(x for x in (agency_name, agency_contact, agency_website) if x)
    agency_html = (
        f'<p style="color:#6b7280;font-size:12px;margin-top:8px">Prepared by {agency_line}</p>' if agency_line else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Website Audit — {business_name}</title></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#fff;color:#111">
<div style="max-width:700px;margin:0 auto;padding:40px 24px">
    <div style="text-align:center;margin-bottom:32px;padding-bottom:24px;border-bottom:2px solid #f3f4f6">
        <h1 style="font-size:24px;font-weight:700;margin-bottom:4px">Website Audit Report</h1>
        <p style="color:#666;font-size:14px">Prepared for {business_name}</p>
        <p style="color:#9ca3af;font-size:12px">{website}</p>
        {agency_html}
    </div>
    <div style="display:flex;justify-content:center;gap:24px;margin-bottom:32px">
        <div style="text-align:center;padding:20px 32px;background:#fafafa;border-radius:6px;border:1px solid #e0e0e0">
            <div style="font-size:36px;font-weight:800;color:{grade_color}">{score}/100</div>
            <div style="font-size:13px;color:#666">Overall Score</div>
        </div>
        <div style="text-align:center;padding:20px 32px;background:#fafafa;border-radius:6px;border:1px solid #e0e0e0">
            <div style="font-size:36px;font-weight:800;color:{grade_color}">{grade}</div>
            <div style="font-size:13px;color:#666">Grade</div>
        </div>
    </div>
    <div style="background:#fafafa;border-radius:6px;padding:20px;margin-bottom:28px;border:1px solid #e0e0e0">
        <h2 style="font-size:16px;font-weight:600;margin-bottom:8px">Executive Summary</h2>
        <p style="color:#374151;font-size:14px">{executive_summary}</p>
    </div>
    {"<div style='background:#f0fdf4;border-radius:6px;padding:20px;margin-bottom:28px;border:1px solid #bbf7d0'><h2 style='font-size:16px;font-weight:600;color:#166534;margin-bottom:12px'>What You're Doing Well</h2>" + highlights_html + "</div>" if highlights_html else ""}
    <h2 style="font-size:18px;margin-bottom:16px">Detailed Findings</h2>
    {sections_html}
    {"<div style='background:#fef3c7;border-radius:6px;padding:20px;margin-bottom:28px;border:1px solid #fbbf24'><h2 style='font-size:16px;font-weight:600;color:#92400e;margin-bottom:12px'>Top Priorities</h2>" + priorities_html + "</div>" if priorities_html else ""}
    {agency_html}
</div></body></html>"""


def _build_tech_summary(technographics: Dict[str, Any]) -> str:
    if not technographics:
        return "No technology data available"
    lines = []
    cms = technographics.get("cms", {})
    cms_name = cms.get("name", "Unknown") if isinstance(cms, dict) else str(cms)
    lines.append(f"CMS: {cms_name}")
    lines.append(f"SSL/HTTPS: {'Active' if technographics.get('ssl') else 'NOT ACTIVE'}")
    lines.append(f"Mobile Responsive: {'Yes' if technographics.get('mobile_responsive') else 'No'}")
    analytics = technographics.get("analytics", {})
    if isinstance(analytics, dict):
        items = []
        if analytics.get("google_analytics"):
            items.append("Google Analytics")
        if analytics.get("meta_pixel"):
            items.append("Meta Pixel")
        lines.append(f"Analytics: {', '.join(items) if items else 'None detected'}")
    lines.append(f"Favicon: {'Present' if technographics.get('favicon') else 'Missing'}")
    return "\n".join(lines)
