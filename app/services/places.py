"""Google Places API — adapted from original, accepts api_key as param."""

import logging
import time
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://maps.googleapis.com/maps/api/place"


def search_places(
    api_key: str,
    business_type: str,
    location: str,
    limit: int = 20,
    page_token: Optional[str] = None,
) -> Dict:
    if not api_key:
        raise ValueError("GOOGLE_MAPS_API_KEY not configured")

    limit = min(limit, 20)
    query = f"{business_type} in {location}"
    url = f"{BASE_URL}/textsearch/json"
    params = {"query": query, "key": api_key}

    if page_token:
        params["pagetoken"] = page_token

    try:
        t0 = time.time()
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        status = data.get("status")
        if status == "REQUEST_DENIED":
            raise ValueError(data.get("error_message", "API key invalid or restricted"))
        elif status == "OVER_QUERY_LIMIT":
            raise ValueError("Google Places API quota exceeded")
        elif status == "ZERO_RESULTS":
            return {"places": [], "next_page_token": None}
        elif status not in ("OK", "INVALID_REQUEST"):
            raise ValueError(f"Google Places API status: {status}")

        results = data.get("results", [])[:limit]
        next_page_token = data.get("next_page_token")

        place_ids = [r["place_id"] for r in results if r.get("place_id")]

        places: List[Dict] = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_get_details, pid, api_key): pid for pid in place_ids}
            try:
                for future in as_completed(futures, timeout=20):
                    try:
                        detail = future.result(timeout=5)
                        if detail:
                            places.append(detail)
                    except Exception as exc:
                        logger.warning("Place detail failed for %s: %s", futures[future], exc)
            except TimeoutError:
                logger.warning("Details deadline exceeded, returning %d partial results", len(places))

        logger.info("Search '%s' returned %d places in %.1fs", query, len(places), time.time() - t0)
        return {"places": places, "next_page_token": next_page_token}

    except requests.exceptions.Timeout:
        raise ValueError("Google Places API request timed out")
    except requests.exceptions.RequestException as exc:
        raise ValueError(f"Network error: {exc}")
    except ValueError:
        raise


def _get_details(place_id: str, api_key: str) -> Optional[Dict]:
    url = f"{BASE_URL}/details/json"
    params = {
        "place_id": place_id,
        "fields": "name,formatted_address,formatted_phone_number,international_phone_number,website,rating,user_ratings_total",
        "key": api_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK":
            return None
        r = data.get("result", {})
        return {
            "name": r.get("name", ""),
            "address": r.get("formatted_address", ""),
            "phone": r.get("formatted_phone_number") or r.get("international_phone_number", ""),
            "website": r.get("website", ""),
            "rating": r.get("rating", 0),
            "review_count": r.get("user_ratings_total", 0),
        }
    except Exception as exc:
        logger.warning("Error getting details for %s: %s", place_id, exc)
        return None
