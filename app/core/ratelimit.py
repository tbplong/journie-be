"""In-memory token buckets: a request rate per client plus one for all clients together.

shortcut: per process, like the other caches; several workers or serverless instances each keep
their own buckets (README: a Vercel Firewall rule is the shared limit in production).
"""

import time
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """At most ``client_per_minute`` requests a minute per client and ``total_per_minute`` in
    all, each bursting up to the same number."""

    def __init__(self, client_per_minute: int, total_per_minute: int) -> None:
        self._client_rate = client_per_minute
        self._total_rate = total_per_minute
        self._total = _Bucket(float(total_per_minute), time.monotonic())
        self._clients: dict[str, _Bucket] = {}

    @staticmethod
    def _wait(bucket: _Bucket, per_minute: int, now: float) -> float:
        """Refills ``bucket`` to ``now``; seconds until it holds one token (0 when it does)."""
        per_second = per_minute / 60
        elapsed = max(0.0, now - bucket.updated)
        bucket.tokens = min(per_minute, bucket.tokens + elapsed * per_second)
        bucket.updated = now
        return 0.0 if bucket.tokens >= 1 else (1 - bucket.tokens) / per_second

    def acquire(self, client: str, now: float | None = None) -> float:
        """Spend one request of ``client``: 0 when allowed, else the seconds to wait (nothing
        is spent then)."""
        now = time.monotonic() if now is None else now
        bucket = self._clients.get(client) or _Bucket(float(self._client_rate), now)
        wait = max(
            self._wait(bucket, self._client_rate, now),
            self._wait(self._total, self._total_rate, now),
        )
        if wait > 0:
            return wait
        bucket.tokens -= 1
        self._total.tokens -= 1
        # A bucket left alone for a minute is full again, the same as a new one: forget it.
        # Only allowed requests add buckets, so the total rate bounds how many are kept.
        self._clients = {
            key: kept for key, kept in self._clients.items() if now - kept.updated < 60
        }
        self._clients[client] = bucket
        return 0.0
