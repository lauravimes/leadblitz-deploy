import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from app.config import get_settings

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")

PHONE_REGEX = re.compile(
    r"(?:"
    r"(?:0\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4})|"
    r"(?:\+44[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4})|"
    r"(?:\+?1?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4})|"
    r"(?:\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4})"
    r")",
    re.VERBOSE,
)

NOREPLY_PATTERNS = ["noreply@", "no-reply@", "donotreply@", "do-not-reply@", "mailer-daemon@"]
PLACEHOLDER_EMAILS = {
    "example@yourmail.com", "test@example.com", "email@example.com",
    "your@email.com", "info@example.com", "user@example.com",
    "admin@example.com", "contact@example.com", "test@test.com",
    "example@example.com", "name@domain.com", "email@domain.com",
}
INVALID_DOMAINS = [
    "example.com", "domain.com", "email.com", "yoursite.com",
    "test.com", "wixpress.com", "sentry.io", "yourmail.com",
]
GENERIC_PREFIXES = ["info", "contact", "hello", "support", "sales", "admin", "enquiries", "mail", "office"]
CONTACT_PAGE_PATHS = ["/contact", "/contact-us", "/about", "/about-us"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}


def extract_domain(website: str) -> Optional[str]:
    try:
        if not website:
            return None
        if not website.startswith(("http://", "https://")):
            website = f"https://{website}"
        parsed = urlparse(website)
        domain = parsed.netloc or parsed.path
        return domain.replace("www.", "")
    except Exception:
        return None


def _fetch_page(url: str, timeout: int = 10) -> Tuple[str, str]:
    try:
        resp = requests.get(url, timeout=timeout, headers=HEADERS, verify=True, allow_redirects=True)
        if resp.status_code == 200:
            return (url, resp.text)
    except Exception:
        pass
    return (url, "")


def _extract_emails_from_html(html: str) -> set:
    emails = set()
    if not html:
        return emails
    found = EMAIL_REGEX.findall(html)
    for e in found:
        emails.add(e.lower().strip())
    # Obfuscated patterns
    for pattern in [
        r"([a-zA-Z0-9._%+-]+)\s*\[\s*at\s*\]\s*([a-zA-Z0-9.-]+)\s*\[\s*dot\s*\]\s*([a-zA-Z]{2,})",
        r"([a-zA-Z0-9._%+-]+)\s*\(\s*at\s*\)\s*([a-zA-Z0-9.-]+)\s*\(\s*dot\s*\)\s*([a-zA-Z]{2,})",
    ]:
        matches = re.findall(pattern, html, re.IGNORECASE)
        for m in matches:
            if isinstance(m, tuple) and len(m) == 3:
                emails.add(f"{m[0]}@{m[1]}.{m[2]}".lower().strip())
    return emails


def _extract_phones_from_html(html: str) -> set:
    phones = set()
    if not html:
        return phones
    for phone in PHONE_REGEX.findall(html):
        cleaned = re.sub(r"[\s\-\(\)]", "", phone)
        if len(cleaned) >= 10:
            phones.add(phone.strip())
    return phones


def _filter_emails(emails: set) -> List[str]:
    filtered = []
    for email in emails:
        if not email or "@" not in email or "." not in email:
            continue
        email_lower = email.lower().strip()
        if email_lower in PLACEHOLDER_EMAILS:
            continue
        if any(p in email_lower for p in NOREPLY_PATTERNS):
            continue
        if any(d in email_lower for d in INVALID_DOMAINS):
            continue
        if email.endswith((".png", ".jpg", ".gif", ".svg", ".webp", ".js", ".css")):
            continue
        filtered.append(email)
    return list(set(filtered))


def extract_emails_from_website(website: str, timeout: int = 10) -> List[str]:
    if not website:
        return []
    try:
        if not website.startswith(("http://", "https://")):
            website = f"https://{website}"
        pages = [website] + [urljoin(website, p) for p in CONTACT_PAGE_PATHS]
        all_html = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_fetch_page, url, timeout): url for url in pages}
            for future in as_completed(futures, timeout=timeout * 2):
                try:
                    _, html = future.result(timeout=timeout + 5)
                    if html:
                        all_html.append(html)
                except Exception:
                    continue
        all_emails = set()
        for html in all_html:
            all_emails.update(_extract_emails_from_html(html))
        return _filter_emails(all_emails)
    except Exception as e:
        logger.error(f"Error extracting emails from {website}: {e}")
        return []


def choose_best_email(candidates: List[str]) -> Optional[str]:
    if not candidates:
        return None
    generic = [e for e in candidates if e.split("@")[0].lower() in GENERIC_PREFIXES]
    return generic[0] if generic else candidates[0]


def extract_phone_from_website(website: str, timeout: int = 10) -> Optional[str]:
    if not website:
        return None
    try:
        if not website.startswith(("http://", "https://")):
            website = f"https://{website}"
        pages = [website] + [urljoin(website, p) for p in CONTACT_PAGE_PATHS]
        all_phones = set()
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_fetch_page, url, timeout): url for url in pages}
            for future in as_completed(futures, timeout=timeout * 2):
                try:
                    _, html = future.result(timeout=timeout + 5)
                    if html:
                        all_phones.update(_extract_phones_from_html(html))
                        tel_matches = re.findall(r'href=["\']tel:([^"\']+)["\']', html, re.IGNORECASE)
                        for tel in tel_matches:
                            cleaned = re.sub(r"[^\d+]", "", tel)
                            if len(cleaned) >= 10:
                                all_phones.add(tel.strip())
                except Exception:
                    continue
        return list(all_phones)[0] if all_phones else None
    except Exception as e:
        logger.error(f"Error extracting phone from {website}: {e}")
        return None


def enrich_from_hunter(domain: str, max_results: int = 3, hunter_api_key: Optional[str] = None) -> Dict:
    api_key = hunter_api_key or get_settings().hunter_api_key
    if not api_key:
        return {"success": False, "error": "Hunter API key not configured", "emails": []}
    try:
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": api_key, "limit": max_results},
            timeout=10,
        )
        if resp.status_code == 401:
            return {"success": False, "error": "Invalid Hunter API key", "emails": []}
        if resp.status_code == 429:
            return {"success": False, "error": "Hunter API rate limit reached", "emails": []}
        if resp.status_code != 200:
            return {"success": False, "error": f"Hunter API error: {resp.status_code}", "emails": []}

        data = resp.json()
        if "data" not in data or "emails" not in data["data"]:
            return {"success": True, "emails": []}

        email_list = []
        for email_obj in data["data"]["emails"]:
            email_addr = email_obj.get("value")
            confidence = email_obj.get("confidence", 0)
            email_type = email_obj.get("type", "")
            if not email_addr:
                continue
            is_generic = email_addr.split("@")[0].lower() in GENERIC_PREFIXES or email_type == "generic"
            if is_generic or confidence >= 50:
                email_list.append({"email": email_addr, "confidence": confidence / 100.0, "type": email_type})
        return {"success": True, "emails": email_list}
    except Exception as e:
        logger.error(f"Hunter API error for {domain}: {e}")
        return {"success": False, "error": str(e), "emails": []}
