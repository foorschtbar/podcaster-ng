"""Admin authentication helpers: signed tokens, credential check, login limiter.

Sessions are stateless signed cookies (HMAC-SHA256). Nothing in here logs
passwords or cookie values.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable

from .config import Config
from .db import connect

SESSION_LIFETIME = 12 * 3600
LOGIN_FORM_LIFETIME = 30 * 60


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: bytes, purpose: str, data: dict, lifetime: int) -> str:
    payload = {**data, "p": purpose, "exp": int(time.time()) + lifetime}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def unsign(secret: bytes, purpose: str, token: str | None) -> dict | None:
    """Return the payload if the signature, purpose and expiry are valid."""
    if not token or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    expected = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("p") != purpose or payload.get("exp", 0) < time.time():
        return None
    return payload


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def check_credentials(config: Config, user: str, password: str) -> bool:
    """Constant-time comparison; both fields are always compared."""
    def digest(value: str) -> bytes:
        return hashlib.sha256(value.encode()).digest()

    user_ok = hmac.compare_digest(digest(user), digest(config.admin_user))
    pass_ok = hmac.compare_digest(digest(password), digest(config.admin_password))
    return user_ok and pass_ok


def load_session_secret(config: Config) -> bytes:
    """SESSION_SECRET if set, otherwise generated once and kept in the settings table."""
    if config.session_secret:
        return config.session_secret.encode()
    conn = connect(config.db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('session_secret', ?)",
            (secrets.token_hex(32),),
        )
        return conn.execute("SELECT value FROM settings WHERE key='session_secret'").fetchone()[0].encode()
    finally:
        conn.close()


class LoginLimiter:
    """In-memory failed-login limiter per client address."""

    def __init__(self, max_failures: int = 5, window: float = 900, lockout: float = 900,
                 delay: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self.delay = delay
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def locked_for(self, client: str) -> int:
        """Remaining lockout in seconds, 0 if not locked."""
        until = self._locked_until.get(client)
        if until is None:
            return 0
        remaining = until - self._clock()
        if remaining <= 0:
            del self._locked_until[client]
            return 0
        return int(remaining) + 1

    def record_failure(self, client: str) -> None:
        now = self._clock()
        if len(self._failures) > 1000:
            self._sweep(now)
        recent = [t for t in self._failures.get(client, []) if now - t < self.window]
        recent.append(now)
        if len(recent) >= self.max_failures:
            self._locked_until[client] = now + self.lockout
            recent = []
        self._failures[client] = recent

    def record_success(self, client: str) -> None:
        self._failures.pop(client, None)

    def _sweep(self, now: float) -> None:
        self._failures = {c: [t for t in ts if now - t < self.window] for c, ts in self._failures.items()}
        self._failures = {c: ts for c, ts in self._failures.items() if ts}
        self._locked_until = {c: u for c, u in self._locked_until.items() if u > now}
