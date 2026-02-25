"""Deterministic website scoring (0-50 points) — near copy from original."""

import re
import json
from typing import Any, Dict, List, Tuple

from bs4 import BeautifulSoup


def decode_obfuscated_email(text: str) -> List[str]:
    emails = []
    patterns = [
        (r'([a-zA-Z0-9._%+-]+)\s*\[\s*at\s*\]\s*([a-zA-Z0-9.-]+)\s*\[\s*dot\s*\]\s*([a-zA-Z]{2,})', r'\1@\2.\3'),
        (r'([a-zA-Z0-9._%+-]+)\s*\(\s*at\s*\)\s*([a-zA-Z0-9.-]+)\s*\(\s*dot\s*\)\s*([a-zA-Z]{2,})', r'\1@\2.\3'),
        (r'([a-zA-Z0-9._%+-]+)\s*@\s*([a-zA-Z0-9.-]+)\s*\.\s*([a-zA-Z]{2,})', r'\1@\2.\3'),
        (r'([a-zA-Z0-9._%+-]+)\s*&#64;\s*([a-zA-Z0-9.-]+)\.([a-zA-Z]{2,})', r'\1@\2.\3'),
    ]
    for pattern, _ in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                emails.append(f"{match[0]}@{match[1]}.{match[2]}".lower())
    return emails


def extract_schema_org_contact(soup: BeautifulSoup) -> Dict[str, Any]:
    contact_info: Dict[str, list] = {"emails": [], "phones": [], "addresses": []}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            if isinstance(data, list):
                for item in data:
                    _extract_from_schema(item, contact_info)
            else:
                _extract_from_schema(data, contact_info)
        except (json.JSONDecodeError, TypeError):
            pass
    return contact_info


def _extract_from_schema(data: dict, contact_info: Dict[str, list]):
    if not isinstance(data, dict):
        return
    if "email" in data:
        email = data["email"].replace("mailto:", "")
        if "@" in email:
            contact_info["emails"].append(email)
    if "telephone" in data:
        contact_info["phones"].append(data["telephone"])
    if "contactPoint" in data:
        cp = data["contactPoint"]
        if isinstance(cp, list):
            for point in cp:
                _extract_from_schema(point, contact_info)
        elif isinstance(cp, dict):
            _extract_from_schema(cp, contact_info)
    if "address" in data:
        addr = data["address"]
        if isinstance(addr, str):
            contact_info["addresses"].append(addr)
        elif isinstance(addr, dict):
            parts = [addr.get("streetAddress", ""), addr.get("addressLocality", ""),
                     addr.get("postalCode", ""), addr.get("addressCountry", "")]
            contact_info["addresses"].append(", ".join(p for p in parts if p))


def detect_contact_forms(soup: BeautifulSoup) -> Tuple[bool, List[str]]:
    form_types: List[str] = []
    for form in soup.find_all("form"):
        form_html = str(form).lower()
        form_text = form.get_text(separator=" ", strip=True).lower()
        if any(kw in form_html or kw in form_text for kw in ["contact", "enquir", "inquiry", "message", "get in touch"]):
            form_types.append("contact_form")
        elif any(kw in form_html or kw in form_text for kw in ["quote", "estimate", "pricing"]):
            form_types.append("quote_form")
        elif any(kw in form_html or kw in form_text for kw in ["book", "appointment", "schedule", "reservation"]):
            form_types.append("booking_form")
        elif any(kw in form_html or kw in form_text for kw in ["subscribe", "newsletter", "signup", "sign up"]):
            form_types.append("newsletter_form")
        email_inputs = form.find_all("input", attrs={"type": "email"})
        text_areas = form.find_all("textarea")
        if (email_inputs or text_areas) and "contact_form" not in form_types:
            form_types.append("generic_form")
    return len(form_types) > 0, list(set(form_types))


def detect_cta_elements(soup: BeautifulSoup) -> Tuple[int, List[str]]:
    cta_keywords = [
        "contact", "call", "get quote", "free quote", "request", "enquire", "inquire",
        "book now", "schedule", "get started", "learn more", "find out", "speak to",
        "talk to", "reach out", "connect", "start now", "try free", "demo", "consultation",
    ]
    cta_texts: List[str] = []
    for button in soup.find_all(["button", "a"]):
        text = button.get_text(strip=True).lower()
        classes = " ".join(button.get("class", [])).lower()
        href = (button.get("href") or "").lower()
        is_cta = (
            any(kw in text for kw in cta_keywords)
            or any(cls in classes for cls in ["cta", "btn-primary", "btn-cta", "action-btn", "contact-btn"])
            or any(kw in href for kw in ["contact", "quote", "book", "schedule", "enquir"])
        )
        if is_cta and text and len(text) < 50:
            cta_texts.append(text[:40])
    return len(cta_texts), cta_texts[:10]


def extract_priority_links(soup: BeautifulSoup, base_url: str) -> List[str]:
    from urllib.parse import urljoin, urlparse

    priority_keywords = [
        "contact", "about", "services", "quote", "book", "enquir",
        "pricing", "get-in-touch", "reach-us", "support", "help",
    ]
    priority_links: List[str] = []
    base_domain = urlparse(base_url).netloc.replace("www.", "")
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        text = link.get_text(strip=True).lower()
        if href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        full_url = urljoin(base_url, href)
        link_domain = urlparse(full_url).netloc.replace("www.", "")
        if link_domain != base_domain:
            continue
        href_lower = href.lower()
        if any(kw in href_lower or kw in text for kw in priority_keywords):
            if full_url not in priority_links:
                priority_links.append(full_url)
    return priority_links[:5]


def score_site_heuristics(html: str, final_url: str = "") -> Dict[str, Any]:
    if not html or len(html.strip()) < 100:
        return {
            "scores": {"mobile": 0, "security": 0, "seo": 0, "contact": 0, "content": 0, "tech": 0},
            "total_heuristic": 0,
            "evidence": {"errors": ["HTML empty or too short"]},
            "rendering_limitations": True,
        }

    soup = BeautifulSoup(html, "html.parser")
    evidence: Dict[str, Any] = {}
    scores = {"mobile": 0, "security": 0, "seo": 0, "contact": 0, "content": 0, "tech": 0}
    rendering_limited = len(html) < 1000

    # 1. Mobile (10 pts)
    viewport = soup.find("meta", attrs={"name": "viewport"})
    if viewport:
        scores["mobile"] += 6
        evidence["viewport"] = str(viewport)[:100]
    buttons = soup.find_all("button")
    large_links = [a for a in soup.find_all("a") if a.get_text(strip=True)]
    if len(buttons) > 0 or len(large_links) > 5:
        scores["mobile"] += 4

    # 2. Security (10 pts)
    if final_url.startswith("https://"):
        scores["security"] += 6
        evidence["https"] = True
    privacy_links = soup.find_all("a", href=re.compile(r"privacy|cookie|gdpr", re.I))
    privacy_text = soup.find_all(string=re.compile(r"privacy policy|cookie policy", re.I))
    if privacy_links or privacy_text:
        scores["security"] += 4

    # 3. SEO (8 pts)
    title_tag = soup.find("title")
    if title_tag and title_tag.get_text(strip=True):
        title_text = title_tag.get_text(strip=True)
        if 10 <= len(title_text) <= 65:
            scores["seo"] += 4
        evidence["title"] = title_text[:100]
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        desc = meta_desc.get("content", "")
        if 50 <= len(desc) <= 170:
            scores["seo"] += 4

    # 4. Contact (8 pts)
    contact_items: List[str] = []
    emails_found: List[str] = []
    phones_found: List[str] = []

    tel_links = soup.find_all("a", href=re.compile(r"^tel:"))
    phone_patterns = soup.find_all(string=re.compile(r"\+?\d{1,4}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}"))
    if tel_links or phone_patterns:
        scores["contact"] += 2
        phones_found.extend([str(t.get("href", ""))[:50] for t in tel_links[:2]])

    mailto_links = soup.find_all("a", href=re.compile(r"^mailto:"))
    for m in mailto_links:
        href = m.get("href", "").replace("mailto:", "").split("?")[0]
        if "@" in href:
            emails_found.append(href)

    text_content = soup.get_text(separator=" ", strip=True)
    emails_found.extend(re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text_content))
    emails_found.extend(decode_obfuscated_email(text_content))

    schema_contact = extract_schema_org_contact(soup)
    emails_found.extend(schema_contact.get("emails", []))
    phones_found.extend(schema_contact.get("phones", []))

    emails_found = list(set(e.lower().strip() for e in emails_found if e and "@" in e))
    if emails_found:
        scores["contact"] += 3
        contact_items.extend([f"email: {e}" for e in emails_found[:3]])
        evidence["emails_found"] = emails_found[:5]

    has_contact_form, form_types = detect_contact_forms(soup)
    if has_contact_form:
        scores["contact"] += 2
        evidence["contact_forms"] = form_types

    address_keywords = soup.find_all(string=re.compile(r"address|location", re.I))
    map_embeds = soup.find_all(["iframe", "div"], attrs={"class": re.compile(r"map", re.I)})
    schema_addresses = schema_contact.get("addresses", [])
    if address_keywords or map_embeds or schema_addresses:
        scores["contact"] += 1

    cta_count, cta_texts = detect_cta_elements(soup)
    if cta_count > 0:
        evidence["cta_buttons"] = cta_texts[:5]
        evidence["cta_count"] = cta_count

    evidence["contact_items"] = contact_items[:8]
    evidence["contact_detection_summary"] = {
        "emails": len(emails_found),
        "phones": len(phones_found),
        "forms": form_types if has_contact_form else [],
        "ctas": cta_count,
    }

    priority_links = extract_priority_links(soup, final_url)
    if priority_links:
        evidence["priority_links"] = priority_links

    # 5. Content (8 pts)
    h1_tags = soup.find_all("h1")
    if h1_tags and h1_tags[0].get_text(strip=True):
        scores["content"] += 4
        evidence["h1"] = h1_tags[0].get_text(strip=True)[:150]
    text_content = soup.get_text(separator=" ", strip=True)
    words = re.findall(r"\b\w+\b", text_content)
    word_count = len(words)
    evidence["text_word_count"] = word_count
    if word_count >= 200:
        scores["content"] += 4

    # 6. Tech (6 pts)
    images = soup.find_all("img")
    modern_images = [img for img in images if img.get("loading") == "lazy" or
                     (img.get("src", "").endswith(".webp") or img.get("src", "").endswith(".avif"))]
    if modern_images:
        scores["tech"] += 3

    social_proof_keywords = ["testimonial", "review", "client", "case study", "award", "certified"]
    if any(soup.find_all(string=re.compile(kw, re.I)) for kw in social_proof_keywords):
        scores["tech"] += 3

    return {
        "scores": scores,
        "total_heuristic": sum(scores.values()),
        "evidence": evidence,
        "rendering_limitations": rendering_limited,
    }
