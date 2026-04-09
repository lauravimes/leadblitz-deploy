"""Google PageSpeed Insights API — mobile performance metrics."""

import logging
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

PAGESPEED_API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


def fetch_mobile_speed(url: str, api_key: str, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """Call PageSpeed Insights API and return mobile performance metrics.

    Returns None on failure (caller should treat speed as unavailable, not broken).
    """
    if not url or not api_key:
        return None

    try:
        resp = requests.get(
            PAGESPEED_API_URL,
            params={
                "url": url,
                "strategy": "mobile",
                "category": "performance",
                "key": api_key,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning("PageSpeed API returned %s for %s", resp.status_code, url)
            return None

        data = resp.json()
        lighthouse = data.get("lighthouseResult", {})
        categories = lighthouse.get("categories", {})
        perf = categories.get("performance", {})
        audits = lighthouse.get("audits", {})

        score_raw = perf.get("score")  # 0.0-1.0
        if score_raw is None:
            return None

        performance_score = round(score_raw * 100)

        # Key Web Vitals
        fcp = audits.get("first-contentful-paint", {})
        lcp = audits.get("largest-contentful-paint", {})
        cls = audits.get("cumulative-layout-shift", {})
        tbt = audits.get("total-blocking-time", {})
        si = audits.get("speed-index", {})

        return {
            "performance_score": performance_score,
            "fcp_ms": fcp.get("numericValue"),
            "fcp_display": fcp.get("displayValue", ""),
            "lcp_ms": lcp.get("numericValue"),
            "lcp_display": lcp.get("displayValue", ""),
            "cls_value": cls.get("numericValue"),
            "cls_display": cls.get("displayValue", ""),
            "tbt_ms": tbt.get("numericValue"),
            "tbt_display": tbt.get("displayValue", ""),
            "si_ms": si.get("numericValue"),
            "si_display": si.get("displayValue", ""),
        }

    except requests.exceptions.Timeout:
        logger.warning("PageSpeed API timed out for %s", url)
        return None
    except Exception:
        logger.exception("PageSpeed API error for %s", url)
        return None
