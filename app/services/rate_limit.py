"""Small in-process sliding-window rate limiter.

Good enough for a single uvicorn worker (which is how LeadBlitz runs). Keys are
arbitrary strings such as ``"login:1.2.3.4"``. Old buckets are pruned globally so
the dict does not grow without bound.
"""

import threading
import time
from collections import defaultdict


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()
        self._last_prune = time.time()

    def _prune(self, now: float) -> None:
        if now - self._last_prune < self.window:
            return
        self._last_prune = now
        cutoff = now - self.window
        for key in list(self._hits.keys()):
            fresh = [t for t in self._hits[key] if t > cutoff]
            if fresh:
                self._hits[key] = fresh
            else:
                del self._hits[key]

    def allow(self, key: str) -> bool:
        """Record a hit for ``key`` and return True if it is within the limit."""
        now = time.time()
        with self._lock:
            self._prune(now)
            cutoff = now - self.window
            hits = [t for t in self._hits[key] if t > cutoff]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def client_ip(request) -> str:
    """Best-effort client IP. Relies on ProxyHeadersMiddleware having rewritten
    ``request.client`` from X-Forwarded-For when behind Render's load balancer."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


# Shared limiters
login_limiter = RateLimiter(limit=10, window_seconds=60)
register_limiter = RateLimiter(limit=5, window_seconds=3600)
forgot_password_limiter = RateLimiter(limit=3, window_seconds=3600)
public_score_ip_limiter = RateLimiter(limit=5, window_seconds=3600)
public_score_user_limiter = RateLimiter(limit=30, window_seconds=3600)
pagespeed_limiter = RateLimiter(limit=20, window_seconds=3600)
