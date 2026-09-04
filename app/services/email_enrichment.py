import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, unquote

import requests

from app.config import get_settings

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")

NOREPLY_PATTERNS = ["noreply@", "no-reply@", "donotreply@", "do-not-reply@", "mailer-daemon@"]
PLACEHOLDER_EMAILS = {
    "example@yourmail.com", "test@example.com", "email@example.com",
    "your@email.com", "info@example.com", "user@example.com",
    "admin@example.com", "contact@example.com", "test@test.com",
    "example@example.com", "name@domain.com", "email@domain.com",
}
# Placeholder / sample domains. Matched on the exact domain or a subdomain of it
# (``x.example.com``) — never as a substring, so ``myemail.com`` is fine.
INVALID_DOMAINS = [
    "example.com", "example.org", "example.net", "domain.com", "email.com",
    "yoursite.com", "test.com", "yourmail.com", "yourdomain.com", "mysite.com",
]
# Site builders / infrastructure whose addresses leak into page footers and JS
# bundles. Never the business's own contact address.
PLATFORM_DOMAINS = [
    "facebook.com", "wix.com", "wixpress.com", "squarespace.com", "godaddy.com",
    "wordpress.com", "wordpress.org", "sentry.io", "sentry-next.wixpress.com",
    "shopify.com", "weebly.com", "webflow.com", "duda.co", "google.com",
    "googleapis.com", "gstatic.com", "w3.org", "schema.org", "jquery.com",
    "cloudflare.com", "mailchimp.com", "hubspot.com",
]
# Regex matches that are really file names (``logo@2x.png``, ``main@1.0.js``).
ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".js", ".css",
    ".woff", ".woff2", ".ttf", ".json", ".map", ".pdf", ".mp4", ".webm",
)
GENERIC_PREFIXES = ["info", "contact", "hello", "support", "sales", "admin", "enquiries", "mail", "office"]
CONTACT_PAGE_PATHS = [
    "/contact", "/contact-us", "/about",
]

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
        domain = (parsed.netloc or parsed.path).split("/")[0].split(":")[0].lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain or None
    except Exception:
        return None


def _domain_matches(domain: str, blocked: str) -> bool:
    return domain == blocked or domain.endswith("." + blocked)


def is_blocked_domain(domain: str) -> bool:
    domain = (domain or "").lower()
    return any(_domain_matches(domain, d) for d in INVALID_DOMAINS + PLATFORM_DOMAINS)


def _fetch_page(url: str, timeout: int = 10) -> Tuple[str, str]:
    """Fetch a page for email scraping. Goes through ``safe_get`` so lead websites
    (which come from Google Places or user CSVs) cannot point us at internal hosts."""
    from app.services.url_safety import safe_get

    try:
        resp = safe_get(url, timeout=timeout, headers=HEADERS, verify=True)
        if resp.status_code == 200:
            return (url, resp.text)
    except Exception:
        pass
    return (url, "")


def _extract_emails_from_html(raw_html: str) -> set:
    emails = set()
    if not raw_html:
        return emails

    # Extract from mailto: links first (most reliable — even if display text is obfuscated)
    mailto_matches = re.findall(r'href=["\']mailto:([^"\'?]+)', raw_html, re.IGNORECASE)
    for addr in mailto_matches:
        addr = unquote(addr).strip()
        if "@" in addr and "." in addr:
            emails.add(addr.lower().strip())

    # Decode HTML entities (&#64; → @, &#46; → ., &commat; → @, etc.)
    decoded = html.unescape(raw_html)

    # Standard regex extraction on both raw and decoded HTML
    for text in [raw_html, decoded]:
        for e in EMAIL_REGEX.findall(text):
            emails.add(e.lower().strip())

    # Obfuscated patterns
    for pattern in [
        r"([a-zA-Z0-9._%+-]+)\s*\[\s*at\s*\]\s*([a-zA-Z0-9.-]+)\s*\[\s*dot\s*\]\s*([a-zA-Z]{2,})",
        r"([a-zA-Z0-9._%+-]+)\s*\(\s*at\s*\)\s*([a-zA-Z0-9.-]+)\s*\(\s*dot\s*\)\s*([a-zA-Z]{2,})",
    ]:
        matches = re.findall(pattern, decoded, re.IGNORECASE)
        for m in matches:
            if isinstance(m, tuple) and len(m) == 3:
                emails.add(f"{m[0]}@{m[1]}.{m[2]}".lower().strip())
    return emails


def _filter_emails(emails: set) -> List[str]:
    filtered = set()
    for email in emails:
        if not email or "@" not in email:
            continue
        email_lower = email.lower().strip()
        local, _, domain = email_lower.rpartition("@")
        if not local or "." not in domain:
            continue
        if email_lower in PLACEHOLDER_EMAILS:
            continue
        if any(p in email_lower for p in NOREPLY_PATTERNS):
            continue
        if is_blocked_domain(domain):
            continue
        if email_lower.endswith(ASSET_SUFFIXES):
            continue
        filtered.add(email_lower)
    return sorted(filtered)


def extract_emails_from_website(website: str, timeout: int = 5) -> List[str]:
    if not website:
        return []
    try:
        if not website.startswith(("http://", "https://")):
            website = f"https://{website}"

        own_domain = extract_domain(website)

        # Try homepage first — many sites have email right there
        _, home_html = _fetch_page(website, timeout=timeout)
        if home_html:
            home_emails = _filter_emails(_extract_emails_from_html(home_html))
            if home_emails:
                return rank_emails(home_emails, own_domain)

        # Fallback: try a few common contact pages in parallel
        pages = [urljoin(website, p) for p in CONTACT_PAGE_PATHS]
        all_emails = set()
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(_fetch_page, url, timeout): url for url in pages}
            try:
                for future in as_completed(futures, timeout=timeout * 2):
                    try:
                        _, html = future.result(timeout=timeout)
                        if html:
                            all_emails.update(_extract_emails_from_html(html))
                    except Exception:
                        continue
            except Exception:
                pass
        return rank_emails(_filter_emails(all_emails), own_domain)
    except Exception as e:
        logger.error(f"Error extracting emails from {website}: {e}")
        return []


def _email_rank(email: str, own_domain: Optional[str]) -> int:
    """Lower is better: own-domain generic (0) > own-domain any (1) >
    other generic (2) > anything else (3)."""
    local, _, domain = email.lower().rpartition("@")
    own = bool(own_domain) and _domain_matches(domain, own_domain)
    generic = local in GENERIC_PREFIXES
    if own and generic:
        return 0
    if own:
        return 1
    if generic:
        return 2
    return 3


def rank_emails(candidates: List[str], own_domain: Optional[str] = None) -> List[str]:
    """Stable sort of candidates, best first (see ``_email_rank``)."""
    own = extract_domain(own_domain) if own_domain else None
    return sorted(candidates, key=lambda e: _email_rank(e, own))


def choose_best_email(candidates: List[str], own_domain: Optional[str] = None) -> Optional[str]:
    """Pick the address most likely to reach the business.

    ``own_domain`` (the lead's website or bare domain) lets an address on the
    business's own domain beat a site builder's or agency's footer address.
    Without it the input order is preserved within each tier, so callers that
    pass ``extract_emails_from_website`` output still get its own-domain-first
    ordering.
    """
    if not candidates:
        return None
    return rank_emails(candidates, own_domain)[0]


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
