"""Audit storage: PostgreSQL when DATABASE_URL is set, otherwise in memory."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    status      TEXT NOT NULL,          -- queued | running | done | failed
    error       TEXT,
    result      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS audits_created_idx ON audits (created_at DESC);
CREATE INDEX IF NOT EXISTS audits_url_idx ON audits (url, created_at DESC);
"""

RESTART_ERROR = "Stopped by a server restart before it finished. Please run the audit again."


class MemoryStore:
    def __init__(self):
        self._rows: dict[str, dict] = {}

    def init(self) -> None:
        pass

    def create(self, url: str) -> str:
        audit_id = uuid.uuid4().hex[:12]
        self._rows[audit_id] = {"id": audit_id, "url": url, "status": "queued", "error": None,
                                "result": None, "created_at": datetime.now(timezone.utc)}
        return audit_id

    def update(self, audit_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
        row = self._rows[audit_id]
        row.update(status=status, result=result, error=error)

    def get(self, audit_id: str) -> dict | None:
        return self._rows.get(audit_id)

    def recent(self, limit: int = 10) -> list[dict]:
        rows = sorted(self._rows.values(), key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]

    def fail_unfinished(self) -> int:
        """Mark audits left queued/running by a restart as failed. Returns how many."""
        n = 0
        for row in self._rows.values():
            if row["status"] in ("queued", "running"):
                row.update(status="failed", error=RESTART_ERROR)
                n += 1
        return n

    def previous(self, url: str, before, exclude_id: str) -> dict | None:
        """The latest finished audit of the same URL that started before `before`."""
        rows = [r for r in self._rows.values()
                if r["url"] == url and r["status"] == "done" and r["id"] != exclude_id
                and r["created_at"] < before]
        return max(rows, key=lambda r: r["created_at"]) if rows else None


class PostgresStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _conn(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=True)

    def init(self) -> None:
        with self._conn() as c:
            c.execute(SCHEMA)

    def create(self, url: str) -> str:
        audit_id = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute("INSERT INTO audits (id, url, status) VALUES (%s, %s, 'queued')", (audit_id, url))
        return audit_id

    def update(self, audit_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE audits SET status=%s, result=%s, error=%s, "
                "finished_at = CASE WHEN %s IN ('done','failed') THEN now() ELSE finished_at END "
                "WHERE id=%s",
                (status, json.dumps(result) if result is not None else None, error, status, audit_id),
            )

    def get(self, audit_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, url, status, error, result, created_at FROM audits WHERE id=%s", (audit_id,)
            ).fetchone()
        if not row:
            return None
        return dict(zip(("id", "url", "status", "error", "result", "created_at"), row))

    def recent(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, url, status, error, result, created_at FROM audits "
                "ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [dict(zip(("id", "url", "status", "error", "result", "created_at"), r)) for r in rows]

    def fail_unfinished(self) -> int:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE audits SET status='failed', error=%s, finished_at=now() "
                "WHERE status IN ('queued','running')", (RESTART_ERROR,))
            return cur.rowcount

    def previous(self, url: str, before, exclude_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, url, status, error, result, created_at FROM audits "
                "WHERE url=%s AND status='done' AND id<>%s AND created_at < %s "
                "ORDER BY created_at DESC LIMIT 1", (url, exclude_id, before)
            ).fetchone()
        return dict(zip(("id", "url", "status", "error", "result", "created_at"), row)) if row else None


def make_store():
    return PostgresStore(config.DATABASE_URL) if config.DATABASE_URL else MemoryStore()
