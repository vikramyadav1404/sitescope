"""Per-IP limit on starting audits.

Each audit crawls up to MAX_PAGES pages from this server, so one client starting
audits in a loop could keep the three crawl slots busy for everyone. This caps
how many audits one IP can start in a rolling hour.

Two backends with the same `retry_after(key)` method:
- RateLimiter keeps hits in memory (dev, tests, no DATABASE_URL).
- PostgresRateLimiter keeps hits in the `rate_hits` table, so the limit survives
  a restart and is shared if more than one app container runs.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import time
from collections import defaultdict, deque

from fastapi import Request

from . import config

log = logging.getLogger("sitescope")

# Cloudflare's published edge ranges (https://www.cloudflare.com/ips/).
# Only a request whose edge address is in this list may tell us the visitor IP
# through CF-Connecting-IP. Update the list if Cloudflare changes it.
CLOUDFLARE_RANGES = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)]


def _ip(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def is_cloudflare(ip) -> bool:
    return ip is not None and any(ip in net for net in CLOUDFLARE_RANGES)


def client_ip(request: Request) -> str:
    """Visitor IP, without trusting headers a client could forge.

    In production the chain is visitor -> Cloudflare -> Nginx (same VM) -> app, and
    the app's port is bound to 127.0.0.1, so every request comes through Nginx.
    1. Nginx always overwrites X-Real-IP with the address that opened the TCP
       connection to the VM (the "edge"), so that header can't be forged.
       request.client is NOT used when X-Real-IP is present: uvicorn runs with
       --proxy-headers and fills it from the first X-Forwarded-For entry, which
       the visitor controls.
    2. If the edge is a Cloudflare address, CF-Connecting-IP holds the real visitor.
       If it is not (someone hit the VM IP directly), CF-Connecting-IP is ignored
       and the edge address itself is the key, so a forged header gains nothing.
    """
    edge = _ip(request.headers.get("x-real-ip"))
    if edge is None:  # local runs and tests without Nginx
        peer = request.client.host if request.client else ""
        edge = _ip(peer)
        if edge is None:
            return peer or "unknown"
    if is_cloudflare(edge):
        visitor = _ip(request.headers.get("cf-connecting-ip"))
        if visitor is not None:
            return str(visitor)
    return str(edge)


class RateLimiter:
    """In-memory sliding window (per app process)."""

    def __init__(self, limit: int, window_s: int = 3600, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def init(self) -> None:
        pass

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


RATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_hits (
    key TEXT        NOT NULL,
    at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rate_hits_key_at_idx ON rate_hits (key, at);
CREATE INDEX IF NOT EXISTS rate_hits_at_idx ON rate_hits (at);
"""


class PostgresRateLimiter:
    """Sliding window stored in Postgres: survives restarts, shared across containers."""

    def __init__(self, dsn: str, limit: int, window_s: int = 3600):
        self.dsn = dsn
        self.limit = limit
        self.window_s = window_s

    def _conn(self):
        import psycopg
        return psycopg.connect(self.dsn)

    def init(self) -> None:
        with self._conn() as c:
            c.execute(RATE_SCHEMA)

    def retry_after(self, key: str) -> int:
        if self.limit <= 0:
            return 0
        try:
            with self._conn() as c:  # one transaction, committed on exit
                # Serialise checks for the same key so two parallel requests
                # cannot both slip under the limit.
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
                c.execute("DELETE FROM rate_hits WHERE at <= now() - make_interval(secs => %s)",
                          (self.window_s,))
                count, age = c.execute(
                    "SELECT count(*), EXTRACT(EPOCH FROM now() - min(at)) "
                    "FROM rate_hits WHERE key=%s", (key,)).fetchone()
                if count >= self.limit:
                    return max(1, math.ceil(self.window_s - float(age or 0)) + 1)
                c.execute("INSERT INTO rate_hits (key) VALUES (%s)", (key,))
                return 0
        except Exception:
            # Fail open: a database hiccup should not block every audit.
            log.exception("rate limit check failed; allowing request")
            return 0


def make_limiter(limit: int | None = None):
    limit = config.AUDITS_PER_IP_PER_HOUR if limit is None else limit
    if config.DATABASE_URL:
        return PostgresRateLimiter(config.DATABASE_URL, limit)
    return RateLimiter(limit)
