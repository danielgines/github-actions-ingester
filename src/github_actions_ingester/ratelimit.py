"""Client-side pacing for GitHub API calls.

Leaky bucket: each ``acquire()`` blocks until at least ``1/rps`` seconds
have passed since the previous acquire. GitHub enforces a primary limit
(5000 req/h for Apps and PATs) and secondary limits on bursts; pacing at
a few requests per second keeps the ingester well inside both while the
primary-limit guard in the client handles the rest.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Thread-safe leaky-bucket pacing helper."""

    def __init__(self, rps: float) -> None:
        if rps <= 0:
            raise ValueError("rps must be > 0")
        self._min_interval = 1.0 / rps
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until the next request slot is allowed.

        The lock guards the slot reservation; the wait happens outside it
        so concurrent acquirers queue up in order instead of serializing
        on the lock for the whole sleep.
        """
        with self._lock:
            now = time.monotonic()
            scheduled_at = max(now, self._last + self._min_interval)
            self._last = scheduled_at
        wait = scheduled_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
