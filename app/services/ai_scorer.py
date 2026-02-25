"""OpenAI GPT-4o scoring — adapted from original, accepts api_key as param."""

import json
import logging
from typing import Any, Dict

from openai import OpenAI

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a website audit expert helping entrepreneurs identify sales opportunities for web development and AI integration services.

Your role: Provide clear, actionable insights in plain English that help the user understand:
1. What this business does well online
2. Where they're falling short
3. Specific opportunities to add value (AI tools, chatbots, modern features, design improvements)

CRITICAL RULES:
1. ONLY use text fragments and elements provided by the caller
2. Do not guess about hidden JS content or features you cannot see
3. If evidence is insufficient, mark "insufficient_evidence": true and reduce confidence
4. Write for a non-technical audience - avoid jargon, use plain English
5. Focus on business impact and sales opportunities"""


def score_with_ai(
    api_key: str,
    site_content: Dict[str, Any],
    heuristic_evidence: Dict[str, Any],
    final_url: str,
    rendering_limitations: bool,
    technographics: Dict[str, Any] = None,
) -> Dict[str, Any]:
    tech_section = ""
    if technographics and technographics.get("detected"):
        cms = technographics.get("cms", {})
        cms_name = cms.get("name", "Unknown")
        cms_version = technographics.get("cms_version")
        analytics = technographics.get("analytics", {})
        jquery = technographics.get("jquery", {})
        social = technographics.get("social_links", {})
        active_socials = [k for k, v in social.items() if v]
        bloat = technographics.get("page_bloat", {})

        tech_section = f"""
TECHNOLOGY STACK DETECTED:
- CMS: {cms_name}{f' version {cms_version}' if cms_version else ''}
- SSL/HTTPS: {'Yes' if technographics.get('ssl') else 'No'}
- Mobile Responsive: {'Yes' if technographics.get('mobile_responsive') else 'No'}
- Google Analytics: {'Yes' if analytics.get('google_analytics') else 'No'}
- Meta/Facebook Pixel: {'Yes' if analytics.get('meta_pixel') else 'No'}
- Other Analytics: {', '.join(analytics.get('other', [])) or 'None'}
- jQuery: {'Yes, version ' + (jquery.get('version') or 'unknown') if jquery.get('present') else 'No'}
- Cookie Consent: {'Yes' if technographics.get('cookie_consent') else 'No'}
- Social Links: {', '.join(active_socials) if active_socials else 'None found'}
- External Resources: {bloat.get('external_scripts', 0)} scripts, {bloat.get('external_stylesheets', 0)} stylesheets
"""

    user_content = f"""Please review this website and provide scores with evidence.

URL: {final_url}
Rendering limitations: {"Yes - content may be incomplete due to JavaScript" if rendering_limitations else "No"}

EXTRACTED CONTENT:
---
Title: {site_content.get('title', 'N/A')}
H1 Headlines: {', '.join(site_content.get('h1_tags', [])) or 'None found'}
H2 Headings: {', '.join(site_content.get('h2_tags', [])[:5]) or 'None found'}
CTA Buttons: {', '.join(site_content.get('cta_buttons', [])) or 'None found'}
Navigation Links: {', '.join(site_content.get('nav_links', [])[:15]) or 'None found'}
Image Alt Texts: {', '.join(site_content.get('image_alts', [])[:5]) or 'None found'}
Link Texts (sample): {', '.join(site_content.get('link_texts', [])[:20]) or 'None found'}

Text Excerpt (first 2000 chars):
{site_content.get('text_excerpt', '')[:2000]}

HEURISTIC FINDINGS:
{json.dumps(heuristic_evidence, indent=2)}
{tech_section}---

SCORING RUBRIC (max 50 points):
1. Brand Clarity (0-12): Is the offer obvious above the fold?
2. Visual Design (0-10): Consistency, whitespace, typography.
3. Conversion UX (0-12): Clear CTAs, contact routes, booking/quote flows.
4. Trust & Proof (0-10): Testimonials, case studies, awards, social proof.
5. Accessibility (0-6): Alt texts, contrast, aria attributes.

Return JSON with:
{{
  "category_scores": {{"brand": 0-12, "visual": 0-10, "conversion": 0-12, "trust": 0-10, "a11y": 0-6}},
  "justifications": {{"brand": "...", "visual": "...", "conversion": "...", "trust": "...", "a11y": "..."}},
  "plain_english_report": {{
    "strengths": ["2-3 specific strengths"],
    "weaknesses": ["2-4 specific weaknesses"],
    "technology_observations": "Paragraph about tech stack",
    "sales_opportunities": ["3-5 specific services to sell"]
  }},
  "insufficient_evidence": false,
  "confidence": 0.0
}}"""

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        result = json.loads(response.choices[0].message.content)

        category_scores = result.get("category_scores", {})
        clamped = {
            "brand": max(0, min(12, category_scores.get("brand", 0))),
            "visual": max(0, min(10, category_scores.get("visual", 0))),
            "conversion": max(0, min(12, category_scores.get("conversion", 0))),
            "trust": max(0, min(10, category_scores.get("trust", 0))),
            "a11y": max(0, min(6, category_scores.get("a11y", 0))),
        }

        insufficient = result.get("insufficient_evidence", False)
        confidence = result.get("confidence", 0.7)

        total_ai = sum(clamped.values())
        if insufficient and total_ai < 20 and heuristic_evidence.get("text_word_count", 0) > 150:
            adj = (20 - total_ai) / 5
            clamped = {k: int(v + adj) for k, v in clamped.items()}

        return {
            "category_scores": clamped,
            "justifications": result.get("justifications", {}),
            "plain_english_report": result.get("plain_english_report", {}),
            "insufficient_evidence": insufficient,
            "confidence": max(0.0, min(1.0, confidence)),
        }

    except Exception as exc:
        logger.exception("AI scoring failed: %s", exc)
        return {
            "category_scores": {"brand": 0, "visual": 0, "conversion": 0, "trust": 0, "a11y": 0},
            "justifications": {"error": f"AI scoring failed: {str(exc)}"},
            "insufficient_evidence": True,
            "confidence": 0.0,
        }


def combine_scores(heuristic: Dict[str, Any], ai_review: Dict[str, Any]) -> Dict[str, Any]:
    heuristic_total = heuristic.get("total_heuristic", 0)
    ai_total = min(50, sum(ai_review.get("category_scores", {}).values()))
    final_score = round(heuristic_total + ai_total)

    word_count = heuristic.get("evidence", {}).get("text_word_count", 0)
    heuristic_confidence = 0.9 if word_count > 150 else 0.6
    ai_confidence = ai_review.get("confidence", 0.6)

    return {
        "final_score": final_score,
        "confidence": round((heuristic_confidence + ai_confidence) / 2, 2),
        "heuristic_score": heuristic_total,
        "ai_score": int(ai_total),
        "breakdown": {
            "heuristic": heuristic.get("scores", {}),
            "ai": ai_review.get("category_scores", {}),
        },
        "evidence": heuristic.get("evidence", {}),
        "ai_justifications": ai_review.get("justifications", {}),
        "plain_english_report": ai_review.get("plain_english_report", {}),
        "rendering_limitations": heuristic.get("rendering_limitations", False),
        "insufficient_evidence": ai_review.get("insufficient_evidence", False),
    }
