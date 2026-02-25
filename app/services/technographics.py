"""Tech stack detection from HTML — near copy from original."""

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup


def detect_technographics(html: str, final_url: str = "", response_headers: Optional[Dict] = None) -> Dict[str, Any]:
    if not html or len(html.strip()) < 50:
        return _empty()

    soup = BeautifulSoup(html, "html.parser")
    html_lower = html.lower()

    return {
        "cms": detect_cms(html_lower, soup),
        "cms_version": detect_cms_version(soup),
        "ssl": detect_ssl(final_url),
        "mobile_responsive": detect_mobile_responsive(soup),
        "analytics": detect_analytics(html_lower, soup),
        "jquery": detect_jquery(html_lower, soup),
        "cookie_consent": detect_cookie_consent(html_lower, soup),
        "social_links": detect_social_links(soup),
        "page_bloat": detect_page_bloat(soup),
        "og_tags": detect_og_tags(soup),
        "favicon": detect_favicon(soup, html_lower),
        "detected": True,
    }


def _empty() -> Dict[str, Any]:
    return {
        "cms": {"name": "Unknown", "confidence": "low"},
        "cms_version": None,
        "ssl": False,
        "mobile_responsive": False,
        "analytics": {"google_analytics": False, "meta_pixel": False, "other": []},
        "jquery": {"present": False, "version": None},
        "cookie_consent": False,
        "social_links": {},
        "page_bloat": {"external_scripts": 0, "external_stylesheets": 0, "total_external": 0},
        "og_tags": {"has_og_title": False, "has_og_image": False},
        "favicon": False,
        "detected": False,
    }


def detect_cms(html_lower: str, soup: BeautifulSoup) -> Dict[str, Any]:
    checks = [
        (["wp-content", "wp-includes"], "WordPress", "high"),
        (["wix.com", "wixsite.com", "_wix_browser_sess"], "Wix", "high"),
        (["squarespace.com", "squarespace-cdn.com"], "Squarespace", "high"),
        (["cdn.shopify.com", "shopify"], "Shopify", "high"),
        (["webflow.com"], "Webflow", "medium"),
        (["/media/jui/", "joomla"], "Joomla", "medium"),
        (["drupal", "/sites/default/files", "/misc/drupal.js"], "Drupal", "medium"),
        (["ghost.io", "ghost-"], "Ghost", "medium"),
        (["weebly.com"], "Weebly", "high"),
        (["godaddy"], "GoDaddy", "medium"),
    ]
    for indicators, name, confidence in checks:
        if any(i in html_lower for i in indicators):
            return {"name": name, "confidence": confidence}

    generator = soup.find("meta", attrs={"name": "generator"})
    if generator:
        gc = (generator.get("content", "") or "").lower()
        for kw, name in [("wordpress", "WordPress"), ("joomla", "Joomla"), ("drupal", "Drupal"), ("wix", "Wix"), ("squarespace", "Squarespace")]:
            if kw in gc:
                return {"name": name, "confidence": "high"}
        if gc.strip():
            return {"name": gc.strip().title(), "confidence": "medium"}

    return {"name": "Custom/Unknown", "confidence": "low"}


def detect_cms_version(soup: BeautifulSoup) -> Optional[str]:
    gen = soup.find("meta", attrs={"name": "generator"})
    if gen:
        content = gen.get("content", "") or ""
        m = re.search(r"[\d]+\.[\d]+(?:\.[\d]+)?", content)
        if m:
            return m.group(0)
    return None


def detect_ssl(final_url: str) -> bool:
    return final_url.lower().startswith("https://")


def detect_mobile_responsive(soup: BeautifulSoup) -> bool:
    return soup.find("meta", attrs={"name": "viewport"}) is not None


def detect_analytics(html_lower: str, soup: BeautifulSoup) -> Dict[str, Any]:
    result: Dict[str, Any] = {"google_analytics": False, "meta_pixel": False, "other": []}
    if any(s in html_lower for s in ["gtag(", "googletagmanager.com", "google-analytics.com", "ga("]):
        result["google_analytics"] = True
    if any(s in html_lower for s in ["connect.facebook.net", "fbq(", "facebook.com/tr"]):
        result["meta_pixel"] = True
    for indicator, name in [("hotjar.com", "Hotjar"), ("clarity.ms", "Microsoft Clarity"), ("plausible.io", "Plausible"), ("matomo", "Matomo"), ("mixpanel.com", "Mixpanel"), ("segment.com", "Segment")]:
        if indicator in html_lower:
            result["other"].append(name)
    return result


def detect_jquery(html_lower: str, soup: BeautifulSoup) -> Dict[str, Any]:
    result: Dict[str, Any] = {"present": False, "version": None}
    if "jquery" in html_lower:
        result["present"] = True
        for pattern in [r"jquery[.-](\d+\.\d+(?:\.\d+)?)", r"jquery\.min\.js\?ver=(\d+\.\d+(?:\.\d+)?)", r"jQuery\s+v?(\d+\.\d+(?:\.\d+)?)"]:
            m = re.search(pattern, html_lower)
            if m:
                result["version"] = m.group(1)
                break
    return result


def detect_cookie_consent(html_lower: str, soup: BeautifulSoup) -> bool:
    indicators = [
        "cookie-consent", "cookieconsent", "cookie-notice", "cookie-banner",
        "cookie-popup", "gdpr-consent", "cc-banner", "cc-window",
        "cookiebot", "osano", "onetrust", "termly", "iubenda",
    ]
    return any(i in html_lower for i in indicators)


def detect_social_links(soup: BeautifulSoup) -> Dict[str, bool]:
    social = {"facebook": False, "instagram": False, "linkedin": False, "twitter": False, "youtube": False, "tiktok": False}
    for link in soup.find_all("a", href=True):
        href = (link.get("href", "") or "").lower()
        if "facebook.com" in href and "/tr" not in href and "sharer" not in href:
            social["facebook"] = True
        if "instagram.com" in href:
            social["instagram"] = True
        if "linkedin.com" in href and "share" not in href:
            social["linkedin"] = True
        if "twitter.com" in href or "x.com/" in href:
            social["twitter"] = True
        if "youtube.com" in href:
            social["youtube"] = True
        if "tiktok.com" in href:
            social["tiktok"] = True
    return social


def detect_page_bloat(soup: BeautifulSoup) -> Dict[str, int]:
    ext_scripts = sum(1 for s in soup.find_all("script", src=True) if s.get("src", "").startswith(("http", "//")))
    ext_css = sum(1 for l in soup.find_all("link", rel="stylesheet") if l.get("href", "").startswith(("http", "//")))
    return {"external_scripts": ext_scripts, "external_stylesheets": ext_css, "total_external": ext_scripts + ext_css}


def detect_og_tags(soup: BeautifulSoup) -> Dict[str, bool]:
    return {
        "has_og_title": soup.find("meta", attrs={"property": "og:title"}) is not None,
        "has_og_image": soup.find("meta", attrs={"property": "og:image"}) is not None,
    }


def detect_favicon(soup: BeautifulSoup, html_lower: str) -> bool:
    if soup.find("link", rel=re.compile(r"icon|shortcut", re.I)):
        return True
    return "favicon" in html_lower


def classify_tech_health(technographics: Dict[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    green: List[Dict[str, str]] = []
    amber: List[Dict[str, str]] = []
    red: List[Dict[str, str]] = []

    if technographics.get("ssl"):
        green.append({"label": "HTTPS", "detail": "SSL secured"})
    else:
        red.append({"label": "No SSL", "detail": "Not using HTTPS"})

    if technographics.get("mobile_responsive"):
        green.append({"label": "Responsive", "detail": "Mobile-friendly"})
    else:
        red.append({"label": "Not Responsive", "detail": "No viewport meta"})

    cms = technographics.get("cms", {})
    cms_name = cms.get("name", "Unknown")
    cms_version = technographics.get("cms_version")
    if cms_name not in ("Custom/Unknown", "Unknown"):
        if cms_version:
            try:
                major = int(cms_version.split(".")[0])
                if cms_name == "WordPress" and major < 6:
                    amber.append({"label": f"{cms_name} {cms_version}", "detail": "Older version"})
                else:
                    green.append({"label": f"{cms_name} {cms_version}", "detail": "CMS detected"})
            except (ValueError, IndexError):
                green.append({"label": cms_name, "detail": "CMS detected"})
        else:
            green.append({"label": cms_name, "detail": "CMS detected"})

    analytics = technographics.get("analytics", {})
    has_analytics = analytics.get("google_analytics") or analytics.get("meta_pixel") or len(analytics.get("other", [])) > 0
    if has_analytics:
        parts = []
        if analytics.get("google_analytics"):
            parts.append("GA")
        if analytics.get("meta_pixel"):
            parts.append("Meta Pixel")
        parts.extend(analytics.get("other", []))
        green.append({"label": "Analytics", "detail": ", ".join(parts[:3])})
    else:
        red.append({"label": "No Analytics", "detail": "No tracking detected"})

    jquery = technographics.get("jquery", {})
    if jquery.get("present"):
        version = jquery.get("version")
        if version:
            try:
                major = int(version.split(".")[0])
                if major < 3:
                    amber.append({"label": f"jQuery {version}", "detail": "Older version"})
                else:
                    green.append({"label": f"jQuery {version}", "detail": "Current version"})
            except (ValueError, IndexError):
                amber.append({"label": "jQuery", "detail": "Version unknown"})
        else:
            amber.append({"label": "jQuery", "detail": "Version unknown"})

    og = technographics.get("og_tags", {})
    if og.get("has_og_title") and og.get("has_og_image"):
        green.append({"label": "OG Tags", "detail": "Social sharing optimised"})
    elif og.get("has_og_title") or og.get("has_og_image"):
        amber.append({"label": "Partial OG", "detail": "Incomplete social tags"})
    else:
        amber.append({"label": "No OG Tags", "detail": "Poor social sharing"})

    if technographics.get("favicon"):
        green.append({"label": "Favicon", "detail": "Browser icon present"})
    else:
        red.append({"label": "No Favicon", "detail": "Missing browser icon"})

    if technographics.get("cookie_consent"):
        green.append({"label": "Cookie Consent", "detail": "GDPR compliance"})

    social = technographics.get("social_links", {})
    active = [k for k, v in social.items() if v]
    if len(active) >= 3:
        green.append({"label": "Social Links", "detail": f"{len(active)} platforms"})
    elif len(active) >= 1:
        amber.append({"label": "Limited Social", "detail": f"Only {len(active)} platform(s)"})

    bloat = technographics.get("page_bloat", {})
    if bloat.get("total_external", 0) > 30:
        amber.append({"label": "Page Bloat", "detail": f"{bloat['total_external']} external resources"})

    return {"green": green, "amber": amber, "red": red}
