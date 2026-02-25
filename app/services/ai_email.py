import logging
from typing import Dict

from openai import OpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)


def generate_personalized_email(lead_data: Dict, base_pitch: str) -> Dict[str, str]:
    s = get_settings()
    if not s.openai_api_key:
        raise ValueError("OpenAI API key is not configured.")

    client = OpenAI(api_key=s.openai_api_key)
    business_name = lead_data.get("name", "your business")
    website = lead_data.get("website", "no website")
    score = lead_data.get("score", 0)

    contact_name = lead_data.get("contact_name", "")
    first_name = contact_name.split()[0] if contact_name and contact_name.strip() else ""
    greeting = f'Start with "Hi {first_name},"' if first_name else 'Start with just "Hi,"'

    prompt = f"""Write a personalized, friendly cold email to a local business.

Business Details:
- Name: {business_name}
- Website: {website}
- Lead Score: {score}/100 (indicates opportunity level)

Your Pitch: {base_pitch}

Requirements:
1. Keep it under 150 words
2. Be specific to THIS business
3. Reference their business name naturally
4. Professional but conversational tone
5. Clear call-to-action
6. NO pushy sales language
7. Make it feel genuine, not templated
8. {greeting}

Return ONLY the email body (no subject line)."""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an expert at writing personalized, non-spammy cold outreach emails."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=300,
    )

    content = response.choices[0].message.content
    body = content.strip() if content else f"Hi,\n\n{base_pitch}\n\nBest regards"

    subject_prompt = f"Write a short, specific email subject line (max 8 words) for an email to {business_name} about: {base_pitch}. Return ONLY the subject line, no quotes or punctuation."
    subject_response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You write compelling email subject lines."},
            {"role": "user", "content": subject_prompt},
        ],
        temperature=0.7,
        max_tokens=20,
    )
    subject_content = subject_response.choices[0].message.content
    subject = subject_content.strip().strip("\"'") if subject_content else f"Opportunity for {business_name}"

    return {"subject": subject, "body": body}
