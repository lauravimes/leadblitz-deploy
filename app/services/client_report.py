import json
import logging
from typing import Any, Dict

from openai import OpenAI

from app.config import get_settings
from app.services.technographics import classify_tech_health

logger = logging.getLogger(__name__)


def generate_client_report(
    lead_data: Dict[str, Any],
    agency_name: str = "",
    agency_website: str = "",
    agency_tagline: str = "",
) -> Dict[str, Any]:
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

        report = json.loads(response.choices[0].message.content)
        report["business_name"] = business_name
        report["website"] = website
        report["score"] = score
        report["agency_name"] = agency_name
        report["agency_website"] = agency_website
        report["agency_tagline"] = agency_tagline
        report["tech_health"] = tech_health
        report["technographics"] = technographics
        return report

    except Exception as e:
        logger.error(f"[client_report] Failed for {business_name}: {e}")
        return {"error": str(e), "business_name": business_name, "website": website, "score": score}


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
    if "error" in report:
        return f"<html><body><h1>Report generation failed</h1><p>{report['error']}</p></body></html>"

    business_name = report.get("business_name", "Business")
    website = report.get("website", "")
    score = report.get("score", 0)
    grade = report.get("overall_grade", "N/A")
    executive_summary = report.get("executive_summary", "")
    sections = report.get("sections", [])
    top_priorities = report.get("top_priorities", [])
    positive_highlights = report.get("positive_highlights", [])

    grade_color = {"A": "#16a34a", "B": "#22c55e", "C": "#eab308", "D": "#f97316", "F": "#ef4444"}.get(grade, "#6b7280")

    sections_html = ""
    for section in sections:
        status = section.get("status", "needs_attention")
        status_color = {"good": "#16a34a", "needs_attention": "#eab308", "critical": "#ef4444"}.get(status, "#6b7280")
        status_label = {"good": "Good", "needs_attention": "Needs Attention", "critical": "Critical"}.get(status, "Unknown")
        sections_html += f"""
        <div style="border:1px solid #e5e7eb;border-radius:6px;padding:20px;margin-bottom:16px;border-left:4px solid {status_color}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <h3 style="margin:0;font-size:16px;color:#111">{section.get('title', '')}</h3>
                <span style="color:{status_color};font-size:12px;font-weight:600">{status_label}</span>
            </div>
            <p style="margin:8px 0;color:#374151;font-size:14px"><strong>Finding:</strong> {section.get('finding', '')}</p>
            <p style="margin:4px 0;color:#6b7280;font-size:13px"><strong>Impact:</strong> {section.get('impact', '')}</p>
            <p style="margin:4px 0;color:#0066ff;font-size:13px;font-weight:500"><strong>Recommendation:</strong> {section.get('recommendation', '')}</p>
        </div>"""

    priorities_html = "".join(
        f'<div style="margin-bottom:8px"><span style="font-weight:700;color:#111">{i}.</span> {p}</div>'
        for i, p in enumerate(top_priorities, 1)
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Website Audit — {business_name}</title></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#fff;color:#111">
<div style="max-width:700px;margin:0 auto;padding:40px 24px">
    <div style="text-align:center;margin-bottom:32px;padding-bottom:24px;border-bottom:2px solid #f3f4f6">
        <h1 style="font-size:24px;font-weight:700;margin-bottom:4px">Website Audit Report</h1>
        <p style="color:#666;font-size:14px">Prepared for {business_name}</p>
        <p style="color:#9ca3af;font-size:12px">{website}</p>
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
    <h2 style="font-size:18px;margin-bottom:16px">Detailed Findings</h2>
    {sections_html}
    {"<div style='background:#fef3c7;border-radius:6px;padding:20px;margin-bottom:28px;border:1px solid #fbbf24'><h2 style='font-size:16px;font-weight:600;color:#92400e;margin-bottom:12px'>Top Priorities</h2>" + priorities_html + "</div>" if priorities_html else ""}
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
