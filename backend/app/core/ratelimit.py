"""In-process sliding-window rate limiter (Requirement 15.6).

Requirement 15.6 caps the login endpoint and the proctoring escalation endpoint
at **5 requests per source IP** and **5 requests per session** within *any
rolling 60-second window*; further requests in that window are rejected with a
429 carrying the standard error envelope until the window resets.

This module provides a small, dependency-free :class:`SlidingWindowRateLimiter`
that keeps, per key, the timestamps of the most recent hits and counts only
those that fall inside the trailing window. It is a true *rolling* window (not a
fixed bucket): every check first evicts timestamps older than ``window_seconds``
so the limit reflects exactly the last 60 seconds at the moment of the request.

The limiter is intentionally in-process (the design is a single-process FastAPI
app for the 72h build) and shaped so it could later be swapped for a Redis-backed
implementation without touching the middleware. It is keyed by an opaque string
so the middleware can register independent limits for ``ip:<addr>`` and
``session:<id>`` against the same instance.

The app factory wires this limiter into a middleware that matches the two
rate-limited paths by prefix (the escalate route is mounted later), so the limit
applies as soon as those routes exist. On rejection the middleware renders the
standard ``{"error": {...}}`` envelope with a 429 status.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# Requirement 15.6 limits: 5 requests per rolling 60-second window.
DEFAULT_MAX_REQUESTS = 5
DEFAULT_WINDOW_SECONDS = 60.0


class SlidingWindowRateLimiter:
    """Count hits per key within a trailing time window and gate on a limit.

    Thread-safe: the FastAPI/Starlette test client and ASGI server may dispatch
    requests from multiple worker threads, so all mutation of the per-key hit
    deques is guarded by a lock. Each key tracks a bounded deque of hit
    timestamps; timestamps older than the window are evicted lazily on every
    check, making the window genuinely rolling.
    """

    def __init__(
        self,
        *,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        time_func=time.monotonic,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max = max_requests
        self._window = window_seconds
        self._now = time_func
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _evict_old(self, hits: "deque[float]", now: float) -> None:
        """Drop timestamps that fell out of the trailing window."""
        cutoff = now - self._window
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def check(self, key: str) -> bool:
        """Record a hit for ``key`` and report whether it is allowed.

        Returns ``True`` when the request is within the limit (and records it),
        ``False`` when accepting it would exceed ``max_requests`` in the current
        rolling window (in which case the hit is *not* recorded, so a blocked
        request does not extend the window).
        """
        now = self._now()
        with self._lock:
            hits = self._hits[key]
            self._evict_old(hits, now)
            if len(hits) >= self._max:
                return False
            hits.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        """Clear recorded hits for ``key`` (or all keys when ``key`` is None)."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


__all__ = [
    "SlidingWindowRateLimiter",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_WINDOW_SECONDS",
]
