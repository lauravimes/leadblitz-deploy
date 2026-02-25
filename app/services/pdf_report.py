import io
import logging
from datetime import datetime
from typing import Any, Dict

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

logger = logging.getLogger(__name__)

PURPLE = colors.HexColor("#7c3aed")
PURPLE_BG = colors.HexColor("#f5f3ff")
GREEN = colors.HexColor("#16a34a")
AMBER = colors.HexColor("#d97706")
RED = colors.HexColor("#dc2626")
DARK = colors.HexColor("#1f2937")
GRAY = colors.HexColor("#6b7280")
LIGHT_GRAY = colors.HexColor("#e5e7eb")
WHITE = colors.white
BG_CARD = colors.HexColor("#f9fafb")


def _get_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("ReportTitle", parent=styles["Title"], fontSize=22, textColor=DARK, spaceAfter=4, fontName="Helvetica-Bold", alignment=TA_CENTER))
    styles.add(ParagraphStyle("ReportSubtitle", parent=styles["Normal"], fontSize=11, textColor=GRAY, spaceAfter=20, alignment=TA_CENTER))
    styles.add(ParagraphStyle("SectionHeader", parent=styles["Heading2"], fontSize=14, textColor=PURPLE, spaceBefore=16, spaceAfter=8, fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle("BodyText2", parent=styles["Normal"], fontSize=10, textColor=DARK, leading=15, spaceAfter=6))
    styles.add(ParagraphStyle("SmallGray", parent=styles["Normal"], fontSize=9, textColor=GRAY, leading=13))
    styles.add(ParagraphStyle("BulletItem", parent=styles["Normal"], fontSize=10, textColor=DARK, leading=15, leftIndent=16, spaceAfter=4))
    styles.add(ParagraphStyle("FindingTitle", parent=styles["Normal"], fontSize=11, textColor=DARK, fontName="Helvetica-Bold", spaceAfter=4))
    styles.add(ParagraphStyle("StatusBadge", parent=styles["Normal"], fontSize=9, fontName="Helvetica-Bold", alignment=TA_RIGHT))
    styles.add(ParagraphStyle("FooterText", parent=styles["Normal"], fontSize=8, textColor=GRAY, alignment=TA_CENTER))
    return styles


def _safe(text: str) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _score_color(score: int):
    if score >= 80: return GREEN
    if score >= 60: return colors.HexColor("#22c55e")
    if score >= 40: return AMBER
    if score >= 20: return colors.HexColor("#f97316")
    return RED


def _grade_from_score(score: int) -> str:
    if score >= 80: return "A"
    if score >= 60: return "B"
    if score >= 40: return "C"
    if score >= 20: return "D"
    return "F"


def _status_color(status: str):
    return {"good": GREEN, "needs_attention": AMBER, "critical": RED}.get(status, GRAY)


def _status_label(status: str) -> str:
    return {"good": "Good", "needs_attention": "Needs Attention", "critical": "Critical"}.get(status, "Unknown")


def _header_footer(canvas, doc, title_text="Website Audit Report", agency_name=""):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(GRAY)
    canvas.drawString(doc.leftMargin, doc.height + doc.topMargin + 12, agency_name or "LeadBlitz")
    canvas.drawRightString(doc.width + doc.leftMargin, doc.height + doc.topMargin + 12, title_text)
    canvas.setStrokeColor(LIGHT_GRAY)
    canvas.line(doc.leftMargin, doc.height + doc.topMargin + 8, doc.width + doc.leftMargin, doc.height + doc.topMargin + 8)
    canvas.drawString(doc.leftMargin, 25, f"Generated {datetime.now().strftime('%B %d, %Y')}")
    canvas.drawRightString(doc.width + doc.leftMargin, 25, f"Page {doc.page}")
    canvas.restoreState()


def _build_score_table(score: int, grade: str, styles):
    sc = _score_color(score)
    data = [
        [
            Paragraph(f'<font size="28" color="{sc.hexval()}">{score}</font><font size="14" color="{GRAY.hexval()}">/100</font>', styles["BodyText2"]),
            Paragraph(f'<font size="28" color="{sc.hexval()}">{grade}</font>', styles["BodyText2"]),
        ],
        [
            Paragraph('<font size="9" color="#6b7280">Overall Score</font>', styles["SmallGray"]),
            Paragraph('<font size="9" color="#6b7280">Grade</font>', styles["SmallGray"]),
        ],
    ]
    t = Table(data, colWidths=[2.5 * inch, 2.5 * inch])
    t.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), BG_CARD),
        ("BOX", (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    return t


def _build_finding_block(section: Dict, styles) -> list:
    status = section.get("status", "needs_attention")
    sc = _status_color(status)
    label = _status_label(status)

    elements = []
    title_row = Table(
        [[Paragraph(f'<b>{_safe(section.get("title", ""))}</b>', styles["FindingTitle"]),
          Paragraph(f'<font color="{sc.hexval()}">[{label}]</font>', styles["StatusBadge"])]],
        colWidths=[4 * inch, 1.8 * inch],
    )
    title_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    elements.append(title_row)

    finding = section.get("finding", "")
    if finding:
        elements.append(Paragraph(f'<b>Finding:</b> {_safe(finding)}', styles["BodyText2"]))
    impact = section.get("impact", "")
    if impact:
        elements.append(Paragraph(f'<font color="#6b7280"><b>Why it matters:</b> {_safe(impact)}</font>', styles["SmallGray"]))
    rec = section.get("recommendation", "")
    if rec:
        elements.append(Paragraph(f'<font color="{PURPLE.hexval()}"><b>Recommendation:</b> {_safe(rec)}</font>', styles["BodyText2"]))

    elements.append(Spacer(1, 4))
    wrapper = Table([[elements]], colWidths=[5.8 * inch])
    wrapper.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WHITE),
        ("BOX", (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("LINEWIDTH", (0, 0), (0, -1), 3),
        ("LINECOLOR", (0, 0), (0, -1), sc),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
    ]))
    return [wrapper, Spacer(1, 8)]


def generate_client_pdf(report_data: Dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    styles = _get_styles()

    business_name = report_data.get("business_name", "Business")
    website = report_data.get("website", "")
    score = report_data.get("score", 0)
    grade = report_data.get("overall_grade", _grade_from_score(score))
    executive_summary = report_data.get("executive_summary", "")
    sections = report_data.get("sections", [])
    top_priorities = report_data.get("top_priorities", [])
    positive_highlights = report_data.get("positive_highlights", [])
    agency_name = report_data.get("agency_name", "")

    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=0.8 * inch, bottomMargin=0.6 * inch, leftMargin=0.75 * inch, rightMargin=0.75 * inch)
    story = []

    story.append(Paragraph("Website Audit Report", styles["ReportTitle"]))
    story.append(Paragraph(f'Prepared for <b>{_safe(business_name)}</b><br/><font size="9" color="#9ca3af">{_safe(website)}</font>', styles["ReportSubtitle"]))
    story.append(Spacer(1, 8))
    story.append(_build_score_table(score, grade, styles))
    story.append(Spacer(1, 16))

    if executive_summary:
        story.append(Paragraph("Executive Summary", styles["SectionHeader"]))
        summary_table = Table([[Paragraph(_safe(executive_summary), styles["BodyText2"])]], colWidths=[5.8 * inch])
        summary_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BG_CARD), ("BOX", (0, 0), (-1, -1), 0.5, LIGHT_GRAY), ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 12), ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14)]))
        story.append(summary_table)
        story.append(Spacer(1, 12))

    if positive_highlights:
        story.append(Paragraph("What You're Doing Well", styles["SectionHeader"]))
        for h in positive_highlights:
            story.append(Paragraph(f'<font color="{GREEN.hexval()}">&#10004;</font> {_safe(h)}', styles["BulletItem"]))
        story.append(Spacer(1, 8))

    if sections:
        story.append(Paragraph("Detailed Findings", styles["SectionHeader"]))
        for section in sections:
            story.extend(_build_finding_block(section, styles))

    if top_priorities:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Top Priorities", styles["SectionHeader"]))
        for i, p in enumerate(top_priorities, 1):
            story.append(Paragraph(f'<font color="{PURPLE.hexval()}"><b>{i}.</b></font> {_safe(p)}', styles["BulletItem"]))

    story.append(Spacer(1, 20))
    story.append(Paragraph("This report was generated automatically based on publicly available website data.", styles["FooterText"]))

    def on_page(canvas, doc_ref):
        _header_footer(canvas, doc_ref, "Website Audit Report", agency_name)

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buffer.getvalue()


def generate_internal_pdf(report_data: Dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    styles = _get_styles()

    business_name = report_data.get("business_name", "Business")
    website = report_data.get("website", "")
    score = report_data.get("score", 0)
    grade = _grade_from_score(score)
    scoring = report_data.get("scoring", {})
    report = report_data.get("report", {})
    tech_health = report_data.get("tech_health", {})

    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=0.8 * inch, bottomMargin=0.6 * inch, leftMargin=0.75 * inch, rightMargin=0.75 * inch)
    story = []

    story.append(Paragraph("Internal Lead Report", styles["ReportTitle"]))
    story.append(Paragraph(f'<b>{_safe(business_name)}</b><br/><font size="9" color="#9ca3af">{_safe(website)}</font>', styles["ReportSubtitle"]))
    story.append(Spacer(1, 8))
    story.append(_build_score_table(score, grade, styles))
    story.append(Spacer(1, 12))

    if scoring:
        story.append(Paragraph("Score Breakdown", styles["SectionHeader"]))
        score_rows = [
            ["Component", "Score"],
            ["Total Score", str(scoring.get("total", score))],
            ["Heuristic Score", str(scoring.get("heuristic", 0))],
            ["AI Score", str(scoring.get("ai", 0))],
            ["Confidence", f"{scoring.get('confidence', 0)}%"],
        ]
        st = Table(score_rows, colWidths=[3 * inch, 2.8 * inch])
        st.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("BACKGROUND", (0, 0), (-1, 0), PURPLE),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("TEXTCOLOR", (0, 1), (-1, -1), DARK),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, BG_CARD]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("BOX", (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
            ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ]))
        story.append(st)
        story.append(Spacer(1, 8))

    if report:
        if report.get("strengths"):
            story.append(Paragraph("Strengths", styles["SectionHeader"]))
            for s_item in report["strengths"]:
                story.append(Paragraph(f'<font color="{GREEN.hexval()}">&#10004;</font> {_safe(str(s_item))}', styles["BulletItem"]))
            story.append(Spacer(1, 6))
        if report.get("weaknesses"):
            story.append(Paragraph("Weaknesses", styles["SectionHeader"]))
            for w in report["weaknesses"]:
                if isinstance(w, dict):
                    story.append(Paragraph(f'<font color="{RED.hexval()}">&#10060;</font> <b>{_safe(w.get("label", ""))}</b>: {_safe(w.get("detail", ""))}', styles["BulletItem"]))
                else:
                    story.append(Paragraph(f'<font color="{RED.hexval()}">&#10060;</font> {_safe(str(w))}', styles["BulletItem"]))
            story.append(Spacer(1, 6))

    if tech_health:
        has_items = any(tech_health.get(k) for k in ["green", "amber", "red"])
        if has_items:
            story.append(Paragraph("Technology Health", styles["SectionHeader"]))
            for item in tech_health.get("green", []):
                lbl = item.get("label", "") if isinstance(item, dict) else str(item)
                detail = item.get("detail", "") if isinstance(item, dict) else ""
                story.append(Paragraph(f'<font color="{GREEN.hexval()}">&#9679;</font> <b>{_safe(lbl)}</b> {_safe(detail)}', styles["BulletItem"]))
            for item in tech_health.get("amber", []):
                lbl = item.get("label", "") if isinstance(item, dict) else str(item)
                detail = item.get("detail", "") if isinstance(item, dict) else ""
                story.append(Paragraph(f'<font color="{AMBER.hexval()}">&#9679;</font> <b>{_safe(lbl)}</b> {_safe(detail)}', styles["BulletItem"]))
            for item in tech_health.get("red", []):
                lbl = item.get("label", "") if isinstance(item, dict) else str(item)
                detail = item.get("detail", "") if isinstance(item, dict) else ""
                story.append(Paragraph(f'<font color="{RED.hexval()}">&#9679;</font> <b>{_safe(lbl)}</b> {_safe(detail)}', styles["BulletItem"]))

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=LIGHT_GRAY))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"Internal report generated on {datetime.now().strftime('%B %d, %Y at %I:%M %p')}. For internal use only.", styles["FooterText"]))

    def on_page(canvas, doc_ref):
        _header_footer(canvas, doc_ref, "Internal Lead Report", "")

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buffer.getvalue()
