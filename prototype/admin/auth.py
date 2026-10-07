"""Password login with server-side sessions (standard library only).

- Passwords are stored as PBKDF2-HMAC-SHA256 (200,000 iterations, random salt).
- On first start, if no user exists, an 'admin' user is created. Its password comes from ADMIN_PASSWORD or
  is generated and printed once on the console (development authentication for the local prototype).
- Session tokens are random (32 bytes), stored only as SHA-256 hashes, sent as an HttpOnly SameSite=Strict
  cookie, and expire after 12 hours. Every state-changing request needs the session's CSRF token.
- Repeated failures for a user name are slowed down (lockout for 5 minutes after 5 failures in 15 minutes).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from ..reception.store import Store, iso, now_jst

ITERATIONS = 200_000
SESSION_S = 12 * 3600
MAX_FAILURES, FAILURE_WINDOW_S, LOCKOUT_S = 5, 15 * 60, 5 * 60
COOKIE = "concierge_session"


def hash_password(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> tuple[str, str, int]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return digest.hex(), salt.hex(), iterations


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def ensure_admin(self, password: str | None = None) -> str | None:
        """Create the first user if there is none. Returns the generated password (to print once) or None."""
        if self.store.q1("SELECT id FROM users LIMIT 1"):
            return None
        generated = None
        if not password:
            password = generated = secrets.token_urlsafe(12)
        h, salt, it = hash_password(password)
        self.store.x("INSERT INTO users(username, pw_hash, salt, iterations, role, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                     ("admin", h, salt, it, "admin", iso(now_jst())))
        self.store.audit("system", "user_create", "admin")
        return generated

    def locked(self, username: str) -> bool:
        with self.lock:
            now = time.time()
            recent = [t for t in self.failures.get(username, []) if now - t < FAILURE_WINDOW_S]
            self.failures[username] = recent
            return len(recent) >= MAX_FAILURES and now - recent[-1] < LOCKOUT_S

    def login(self, username: str, password: str) -> tuple[str, str] | None:
        if self.locked(username):
            raise PermissionError("ログインの失敗が続いたため、5分間お待ちください")
        row = self.store.q1("SELECT * FROM users WHERE username = ?", (username,))
        salt = bytes.fromhex(row["salt"]) if row else b"\0" * 16
        digest, _, _ = hash_password(password, salt, row["iterations"] if row else ITERATIONS)
        if not row or not hmac.compare_digest(digest, row["pw_hash"]):
            with self.lock:
                self.failures.setdefault(username, []).append(time.time())
            self.store.audit(username, "login_failed")
            return None
        with self.lock:
            self.failures.pop(username, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.store.x("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        self.store.x("INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
                     (_token_hash(token), row["id"], csrf, iso(now_jst()), time.time() + SESSION_S))
        self.store.audit(username, "login")
        return token, csrf

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.store.q1("SELECT s.csrf, s.expires_at, u.username, u.role FROM sessions s JOIN users u "
                            "ON u.id = s.user_id WHERE s.token_hash = ?", (_token_hash(token),))
        if not row or row["expires_at"] < time.time():
            return None
        return {"user": row["username"], "role": row["role"], "csrf": row["csrf"]}

    def logout(self, token: str | None) -> None:
        if token:
            self.store.x("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
