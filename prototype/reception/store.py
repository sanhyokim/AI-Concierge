"""SQLite storage for the reception app (standard library only).

One file holds settings (versioned), FAQ, calls with their fields and events, staff corrections,
summaries, notification targets and the notification outbox, users and sessions, and the audit log.
Writes go through one connection guarded by a lock; ``tx()`` groups writes into one transaction so a
call record and its outbox rows are committed together.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import threading
from contextlib import contextmanager
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_DB = pathlib.Path(__file__).resolve().parents[1] / "data" / "reception.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS config_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, created_by TEXT, note TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS faqs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, question TEXT NOT NULL, answer TEXT NOT NULL,
  keywords TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, approval TEXT NOT NULL DEFAULT '未承認',
  generated TEXT, position INTEGER NOT NULL DEFAULT 0, updated_at TEXT, updated_by TEXT);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
  iterations INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'admin', created_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf TEXT NOT NULL, created_at TEXT, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS calls(
  id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, end_reason TEXT,
  dialed TEXT, caller_id TEXT, route TEXT NOT NULL, route_rule TEXT, route_reason TEXT, config_version INTEGER,
  snapshot TEXT NOT NULL, flow TEXT, state TEXT NOT NULL, notify_kinds TEXT NOT NULL DEFAULT '[]',
  open_questions TEXT NOT NULL DEFAULT '[]', emergency TEXT, handled_status TEXT NOT NULL DEFAULT '未対応');
CREATE TABLE IF NOT EXISTS call_fields(
  call_id TEXT NOT NULL, name TEXT NOT NULL, value TEXT, status TEXT NOT NULL, source TEXT, read_back_value TEXT,
  history TEXT NOT NULL DEFAULT '[]', updated_at TEXT, PRIMARY KEY(call_id, name));
CREATE TABLE IF NOT EXISTS call_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS call_events_call ON call_events(call_id, id);
CREATE TABLE IF NOT EXISTS corrections(
  id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL, field TEXT NOT NULL, before_value TEXT,
  before_status TEXT, after_value TEXT, after_status TEXT NOT NULL, reason TEXT NOT NULL, by_user TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summaries(
  call_id TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL, items TEXT NOT NULL, review TEXT NOT NULL,
  reason TEXT, created_at TEXT NOT NULL, PRIMARY KEY(call_id, version));
CREATE TABLE IF NOT EXISTS notify_targets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, channel TEXT NOT NULL, address TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, simulate TEXT NOT NULL DEFAULT 'success', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ident TEXT UNIQUE NOT NULL, call_id TEXT NOT NULL, target_id INTEGER NOT NULL,
  version INTEGER NOT NULL, kind TEXT NOT NULL, retry_key TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, first_attempt_at TEXT, next_attempt_at TEXT, last_error TEXT,
  accepted_request_id TEXT, created_at TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS notification_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER NOT NULL, at TEXT NOT NULL, retry_key TEXT NOT NULL,
  outcome TEXT NOT NULL, http_status INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS channel_state(channel TEXT PRIMARY KEY, stopped INTEGER NOT NULL DEFAULT 0, reason TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, user TEXT, action TEXT NOT NULL, target TEXT, detail TEXT);
"""


def now_jst() -> dt.datetime:
    return dt.datetime.now(JST)


def iso(t: dt.datetime) -> str:
    return t.astimezone(JST).isoformat(timespec="seconds")


def parse_at(value: str | dt.datetime | None, default: dt.datetime | None = None) -> dt.datetime:
    """A time from the API or a test. A value without an offset is read as Japan time (JST)."""
    if value is None or value == "":
        return (default or now_jst()).astimezone(JST)
    t = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t.replace(tzinfo=JST) if t.tzinfo is None else t.astimezone(JST)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class Store:
    def __init__(self, path: str | pathlib.Path = DEFAULT_DB) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            pathlib.Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._depth = 0
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """One transaction; nested calls join the outer one."""
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self.db.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self.db.execute("COMMIT")

    def x(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, params)

    def q(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def q1(self, sql: str, params=()) -> dict | None:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def meta(self, key: str, value: str | None = None) -> str | None:
        if value is not None:
            self.x("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, value))
            return value
        row = self.q1("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def audit(self, user: str | None, action: str, target: str = "", detail=None) -> None:
        self.x("INSERT INTO audit_log(at, user, action, target, detail) VALUES(?, ?, ?, ?, ?)",
               (iso(now_jst()), user, action, target, dumps(detail) if detail is not None else None))

    def close(self) -> None:
        with self.lock:
            self.db.close()
