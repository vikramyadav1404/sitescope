"""Accounts, sessions and monitored sites (scheduled re-audits).

Passwords are hashed with scrypt from the standard library, so no new dependency.
Session and email-verification tokens are random; only their SHA-256 is stored,
so a database leak does not hand out working sessions.

Storage mirrors store.py: Postgres when DATABASE_URL is set, otherwise memory.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from . import config

SESSION_DAYS = 30
FREQUENCIES = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}

# --- passwords and tokens -----------------------------------------------------

_N, _R, _P = 2 ** 14, 8, 1  # ~16 MB per hash: fine on a 1 GiB VM


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                            n=int(n), r=int(r), p=int(p), dklen=len(want) // 2)
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


# Used to spend the same time on unknown emails as on a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def new_token() -> tuple[str, str]:
    """(token for the user, hash to store)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    if len(email) > 254 or email.count("@") != 1:
        return False
    local, domain = email.split("@")
    return bool(local) and "." in domain and not any(c.isspace() for c in email)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex[:12]


# --- storage ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    pw_hash     TEXT NOT NULL,
    verified    BOOLEAN NOT NULL DEFAULT false,
    verify_hash TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS monitors (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    frequency     TEXT NOT NULL,          -- daily | weekly
    next_run_at   TIMESTAMPTZ NOT NULL,
    last_audit_id TEXT,
    last_score    INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, url)
);
CREATE INDEX IF NOT EXISTS monitors_due_idx ON monitors (next_run_at);
"""

USER_COLS = ("id", "email", "pw_hash", "verified")
MON_COLS = ("id", "user_id", "url", "frequency", "next_run_at", "last_audit_id", "last_score", "created_at")


class MemoryAccounts:
    def __init__(self):
        self.users: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.monitors: dict[str, dict] = {}

    def init(self) -> None:
        pass

    # users
    def create_user(self, email: str, pw_hash: str, verify_hash: str | None) -> str | None:
        if any(u["email"] == email for u in self.users.values()):
            return None
        uid = _id()
        self.users[uid] = {"id": uid, "email": email, "pw_hash": pw_hash, "verified": False,
                           "verify_hash": verify_hash}
        return uid

    def user_by_email(self, email: str) -> dict | None:
        return next((u for u in self.users.values() if u["email"] == email), None)

    def verify(self, verify_hash: str) -> bool:
        for u in self.users.values():
            if u["verify_hash"] and hmac.compare_digest(u["verify_hash"], verify_hash):
                u.update(verified=True, verify_hash=None)
                return True
        return False

    def delete_user(self, user_id: str) -> None:
        self.users.pop(user_id, None)
        self.sessions = {k: v for k, v in self.sessions.items() if v["user_id"] != user_id}
        self.monitors = {k: v for k, v in self.monitors.items() if v["user_id"] != user_id}

    # sessions
    def create_session(self, user_id: str, th: str) -> None:
        self.sessions[th] = {"user_id": user_id, "expires_at": _now() + timedelta(days=SESSION_DAYS)}

    def session_user(self, th: str) -> dict | None:
        s = self.sessions.get(th)
        if not s or s["expires_at"] <= _now():
            return None
        return self.users.get(s["user_id"])

    def delete_session(self, th: str) -> None:
        self.sessions.pop(th, None)

    # monitors
    def add_monitor(self, user_id: str, url: str, frequency: str) -> str | None:
        if any(m["user_id"] == user_id and m["url"] == url for m in self.monitors.values()):
            return None
        mid = _id()
        self.monitors[mid] = {"id": mid, "user_id": user_id, "url": url, "frequency": frequency,
                              "next_run_at": _now(), "last_audit_id": None, "last_score": None,
                              "created_at": _now()}
        return mid

    def monitors_for(self, user_id: str) -> list[dict]:
        return sorted((m for m in self.monitors.values() if m["user_id"] == user_id),
                      key=lambda m: m["created_at"])

    def delete_monitor(self, user_id: str, mid: str) -> None:
        m = self.monitors.get(mid)
        if m and m["user_id"] == user_id:
            del self.monitors[mid]

    def run_now(self, user_id: str, mid: str) -> bool:
        m = self.monitors.get(mid)
        if not m or m["user_id"] != user_id:
            return False
        m["next_run_at"] = _now()
        return True

    def claim_due(self, limit: int) -> list[dict]:
        """Due monitors, with next_run_at already moved forward so no one else takes them."""
        now = _now()
        due = sorted((m for m in self.monitors.values() if m["next_run_at"] <= now),
                     key=lambda m: m["next_run_at"])[:limit]
        out = []
        for m in due:
            m["next_run_at"] = now + FREQUENCIES[m["frequency"]]
            u = self.users[m["user_id"]]
            out.append({**m, "email": u["email"], "verified": u["verified"]})
        return out

    def set_result(self, mid: str, audit_id: str, score: int | None) -> None:
        m = self.monitors.get(mid)
        if m:
            m["last_audit_id"] = audit_id
            if score is not None:
                m["last_score"] = score


class PostgresAccounts:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _conn(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=True)

    def init(self) -> None:
        with self._conn() as c:
            c.execute(SCHEMA)

    def create_user(self, email: str, pw_hash: str, verify_hash: str | None) -> str | None:
        uid = _id()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO users (id, email, pw_hash, verify_hash) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (email) DO NOTHING", (uid, email, pw_hash, verify_hash))
            return uid if cur.rowcount == 1 else None

    def user_by_email(self, email: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT id, email, pw_hash, verified FROM users WHERE email=%s",
                            (email,)).fetchone()
        return dict(zip(USER_COLS, row)) if row else None

    def verify(self, verify_hash: str) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE users SET verified=true, verify_hash=NULL WHERE verify_hash=%s",
                            (verify_hash,))
            return cur.rowcount == 1

    def delete_user(self, user_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM users WHERE id=%s", (user_id,))  # sessions, monitors cascade

    def create_session(self, user_id: str, th: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires_at <= now()")
            c.execute("INSERT INTO sessions (token_hash, user_id, expires_at) "
                      "VALUES (%s, %s, now() + make_interval(days => %s))", (th, user_id, SESSION_DAYS))

    def session_user(self, th: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT u.id, u.email, u.pw_hash, u.verified FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash=%s AND s.expires_at > now()", (th,)).fetchone()
        return dict(zip(USER_COLS, row)) if row else None

    def delete_session(self, th: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=%s", (th,))

    def add_monitor(self, user_id: str, url: str, frequency: str) -> str | None:
        mid = _id()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO monitors (id, user_id, url, frequency, next_run_at) "
                "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (user_id, url) DO NOTHING",
                (mid, user_id, url, frequency))
            return mid if cur.rowcount == 1 else None

    def monitors_for(self, user_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(f"SELECT {', '.join(MON_COLS)} FROM monitors WHERE user_id=%s "
                             "ORDER BY created_at", (user_id,)).fetchall()
        return [dict(zip(MON_COLS, r)) for r in rows]

    def delete_monitor(self, user_id: str, mid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM monitors WHERE id=%s AND user_id=%s", (mid, user_id))

    def run_now(self, user_id: str, mid: str) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE monitors SET next_run_at=now() WHERE id=%s AND user_id=%s",
                            (mid, user_id))
            return cur.rowcount == 1

    def claim_due(self, limit: int) -> list[dict]:
        # SKIP LOCKED: if a second app container runs, each due monitor is taken once.
        with self._conn() as c:
            rows = c.execute(
                "UPDATE monitors m SET next_run_at = now() + CASE m.frequency "
                "  WHEN 'daily' THEN interval '1 day' ELSE interval '7 days' END "
                "FROM users u WHERE u.id = m.user_id AND m.id IN ("
                "  SELECT id FROM monitors WHERE next_run_at <= now() "
                "  ORDER BY next_run_at LIMIT %s FOR UPDATE SKIP LOCKED) "
                f"RETURNING {', '.join('m.' + col for col in MON_COLS)}, u.email, u.verified",
                (limit,)).fetchall()
        return [dict(zip(MON_COLS + ("email", "verified"), r)) for r in rows]

    def set_result(self, mid: str, audit_id: str, score: int | None) -> None:
        with self._conn() as c:
            c.execute("UPDATE monitors SET last_audit_id=%s, last_score=COALESCE(%s, last_score) "
                      "WHERE id=%s", (audit_id, score, mid))


def make_accounts():
    return PostgresAccounts(config.DATABASE_URL) if config.DATABASE_URL else MemoryAccounts()
