"""HTTP fetch with retries — adapted from original, removed prints, added logging."""

import logging
import random
import time
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)


def fetch_site_safely(url: str, timeout: int = 15, max_retries: int = 3) -> Dict[str, Any]:
    user_agents = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    ]

    result = {"status": None, "html": "", "final_url": url, "errors": [], "retries": 0}
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"

    for attempt in range(max_retries):
        headers = {
            "User-Agent": random.choice(user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Cache-Control": "no-cache",
            "Referer": "https://www.google.com/",
            "Origin": domain,
        }

        try:
            response = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True, verify=True)
            result["status"] = response.status_code
            result["final_url"] = response.url
            result["retries"] = attempt

            if response.status_code in [200, 202]:
                if response.text and len(response.text) > 500:
                    sample = response.text[:500]
                    suspicious = sum(1 for c in sample if ord(c) < 32 and c not in "\n\r\t")
                    if suspicious > 20:
                        logger.debug("Garbled response for %s, retrying without compression", url)
                        try:
                            clean_headers = headers.copy()
                            clean_headers.pop("Accept-Encoding", None)
                            clean_resp = requests.get(url, timeout=timeout, headers=clean_headers, allow_redirects=True, verify=True)
                            if clean_resp.status_code == 200 and clean_resp.text:
                                result["html"] = clean_resp.text
                                result["status"] = clean_resp.status_code
                                result["final_url"] = clean_resp.url
                                return result
                        except Exception:
                            pass
                        if attempt < max_retries - 1:
                            time.sleep(1.5)
                            continue

                if response.text and len(response.text) > 500:
                    result["html"] = response.text
                    result["errors"] = []
                    return result
                elif response.status_code == 202:
                    result["errors"].append("HTTP 202 (needs browser rendering)")
                    return result
                else:
                    result["html"] = response.text
                    return result

            elif response.status_code in [429, 503]:
                result["errors"].append(f"HTTP {response.status_code}")
                if attempt < max_retries - 1:
                    time.sleep(2**attempt + random.uniform(0.5, 1.5))
                    continue
            elif response.status_code in [403, 401]:
                result["errors"].append(f"HTTP {response.status_code} (blocked)")
                return result
            else:
                result["errors"].append(f"HTTP {response.status_code}")
                return result

        except requests.exceptions.Timeout:
            result["errors"].append(f"Timeout (attempt {attempt + 1})")
            if attempt < max_retries - 1:
                time.sleep(1 + attempt)
                continue
        except requests.exceptions.SSLError:
            try:
                response = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True, verify=False)
                if response.status_code == 200:
                    result["html"] = response.text
                    result["status"] = response.status_code
                    result["final_url"] = response.url
                    result["errors"] = ["SSL warning (insecure)"]
                    return result
            except Exception:
                pass
            result["errors"].append("SSL certificate error")
            return result
        except requests.exceptions.ConnectionError:
            result["errors"].append(f"Connection failed (attempt {attempt + 1})")
            if attempt < max_retries - 1:
                time.sleep(1 + attempt)
                continue
        except requests.exceptions.TooManyRedirects:
            result["errors"].append("Too many redirects")
            return result
        except Exception as exc:
            result["errors"].append(f"Fetch error: {str(exc)[:100]}")
            return result

        result["retries"] = attempt + 1

    return result


def fetch_multiple_pages(base_url: str, max_pages: int = 4) -> Dict[str, Any]:
    from bs4 import BeautifulSoup

    fetched_pages = {}
    combined_html = ""
    all_errors = []
    priority_links_found = []

    homepage_result = fetch_site_safely(base_url)
    fetched_pages["homepage"] = homepage_result

    if homepage_result["html"]:
        combined_html += f"\n\n<!-- Page: homepage -->\n{homepage_result['html']}"
        soup = BeautifulSoup(homepage_result["html"], "html.parser")
        priority_links_found = _extract_priority_links_from_soup(soup, homepage_result.get("final_url", base_url))

    if homepage_result["errors"]:
        all_errors.extend([f"homepage: {err}" for err in homepage_result["errors"]])

    fallback_pages = [
        ("contact", urljoin(base_url, "/contact")),
        ("contact-us", urljoin(base_url, "/contact-us")),
        ("get-in-touch", urljoin(base_url, "/get-in-touch")),
        ("about", urljoin(base_url, "/about")),
    ]

    def link_priority(link):
        ll = link.lower()
        if "contact" in ll or "get-in-touch" in ll or "reach-us" in ll:
            return 0
        if "quote" in ll or "enquir" in ll or "book" in ll:
            return 1
        if "pricing" in ll or "schedule" in ll:
            return 2
        if "about" in ll or "services" in ll:
            return 3
        return 4

    sorted_priority = sorted(priority_links_found, key=link_priority)

    pages_to_fetch = []
    for link in sorted_priority[:3]:
        ll = link.lower()
        name = "priority_link"
        if "contact" in ll or "get-in-touch" in ll:
            name = "contact"
        elif "quote" in ll or "pricing" in ll:
            name = "quote"
        elif "about" in ll:
            name = "about"
        elif "book" in ll or "schedule" in ll:
            name = "booking"
        pages_to_fetch.append((name, link))

    fetched_urls = {base_url.rstrip("/"), homepage_result.get("final_url", base_url).rstrip("/")}

    if len(pages_to_fetch) < max_pages - 1:
        for name, url in fallback_pages:
            if url.rstrip("/") not in fetched_urls and len(pages_to_fetch) < max_pages - 1:
                pages_to_fetch.append((name, url))

    for page_name, url in pages_to_fetch[: max_pages - 1]:
        if url.rstrip("/") in fetched_urls:
            continue
        result = fetch_site_safely(url)
        if result["status"] == 404:
            continue
        fetched_pages[page_name] = result
        fetched_urls.add(url.rstrip("/"))
        if result["html"]:
            combined_html += f"\n\n<!-- Page: {page_name} -->\n{result['html']}"
        if result["errors"]:
            all_errors.extend([f"{page_name}: {err}" for err in result["errors"]])

    return {
        "pages": fetched_pages,
        "combined_html": combined_html or homepage_result.get("html", ""),
        "final_url": homepage_result.get("final_url", base_url),
        "status": homepage_result.get("status"),
        "errors": all_errors,
        "priority_links_discovered": priority_links_found[:5],
    }


def _extract_priority_links_from_soup(soup, base_url: str) -> List[str]:
    priority_keywords = [
        "contact", "quote", "book", "enquir", "pricing",
        "get-in-touch", "reach-us", "schedule", "about", "services",
    ]
    priority_links = []
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

    return priority_links[:8]


def extract_site_content_for_ai(html: str, max_chars: int = 6000) -> Dict[str, Any]:
    from bs4 import BeautifulSoup
    import re

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    title = soup.find("title")
    title_text = title.get_text(strip=True) if title else ""

    h1_texts = [h.get_text(strip=True) for h in soup.find_all("h1", limit=3) if h.get_text(strip=True)]
    h2_texts = [h.get_text(strip=True) for h in soup.find_all("h2", limit=5) if h.get_text(strip=True)]

    buttons = soup.find_all("button", limit=10)
    cta_links = soup.find_all("a", class_=re.compile(r"btn|button|cta", re.I), limit=10)
    cta_texts = [e.get_text(strip=True) for e in buttons + cta_links if e.get_text(strip=True) and len(e.get_text(strip=True)) < 50]

    nav = soup.find("nav") or soup.find("header")
    nav_links = []
    if nav and hasattr(nav, "find_all"):
        nav_links = [a.get_text(strip=True) for a in nav.find_all("a", limit=15) if a.get_text(strip=True)]

    image_alts = [img.get("alt", "") for img in soup.find_all("img", limit=10) if img.get("alt")]

    text_content = soup.get_text(separator=" ", strip=True)
    text_excerpt = text_content[:max_chars]

    link_texts = [a.get_text(strip=True) for a in soup.find_all("a", limit=30) if a.get_text(strip=True)]

    return {
        "title": title_text,
        "h1_tags": h1_texts,
        "h2_tags": h2_texts,
        "cta_buttons": cta_texts[:10],
        "nav_links": nav_links[:15],
        "image_alts": image_alts[:10],
        "text_excerpt": text_excerpt,
        "link_texts": link_texts[:30],
    }
