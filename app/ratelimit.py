"""Per-IP limit on starting audits (in memory, per app process).

Each audit crawls up to MAX_PAGES pages from this server, so one client starting
audits in a loop could keep the three crawl slots busy for everyone. This caps
how many audits one IP can start in a rolling hour.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request


def client_ip(request: Request) -> str:
    """Visitor IP. Cloudflare sends the real one in CF-Connecting-IP; without it
    (local runs, tests) fall back to the address uvicorn saw."""
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    return request.client.host if request.client else "unknown"


class RateLimiter:
    def __init__(self, limit: int, window_s: int = 3600, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def retry_after(self, key: str) -> int:
        """0 and the hit is recorded if allowed; otherwise seconds until the next slot."""
        if self.limit <= 0:
            return 0
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return max(1, int(self.window_s - (now - hits[0])) + 1)
        hits.append(now)
        if len(self._hits) > 10_000:  # drop idle keys so memory stays bounded
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window_s]:
                del self._hits[k]
        return 0
