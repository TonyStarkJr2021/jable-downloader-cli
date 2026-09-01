from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return a portable scrypt password record without storing plaintext."""
    if not 12 <= len(password) <= 256:
        raise ValueError("密码长度必须在 12 到 256 个字符之间")
    salt = salt or secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32
    )
    return f"scrypt${n}${r}${p}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, record: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = record.split("$", 5)
        if algorithm != "scrypt" or len(password) > 256:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_b64decode(expected)),
        )
        return hmac.compare_digest(actual, _b64decode(expected))
    except (ValueError, TypeError):
        return False


@dataclass
class Session:
    username: str
    csrf_token: str
    expires_at: float


class SessionStore:
    def __init__(self, timeout_seconds: int = 43200) -> None:
        self.timeout_seconds = timeout_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        session = Session(
            username=username,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=time.time() + self.timeout_seconds,
        )
        with self._lock:
            self._sessions[token] = session
            self._purge_locked()
        return token, session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(token)
            if session is None or session.expires_at <= now:
                self._sessions.pop(token, None)
                return None
            session.expires_at = now + self.timeout_seconds
            return session

    def delete(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def keep_only(self, token: str | None, username: str) -> None:
        """Revoke every other session and refresh the current display name."""
        if not token:
            return
        with self._lock:
            session = self._sessions.get(token)
            self._sessions.clear()
            if session is not None:
                session.username = username
                self._sessions[token] = session

    def _purge_locked(self) -> None:
        now = time.time()
        expired = [token for token, value in self._sessions.items() if value.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)


class LoginLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 900) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, key: str) -> bool:
        with self._lock:
            attempts = self._recent_locked(key)
            return len(attempts) < self.max_attempts

    def fail(self, key: str) -> None:
        with self._lock:
            attempts = self._recent_locked(key)
            attempts.append(time.time())
            self._attempts[key] = attempts

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def retry_after(self, key: str) -> int:
        with self._lock:
            attempts = self._recent_locked(key)
            if len(attempts) < self.max_attempts:
                return 0
            return max(1, int(attempts[0] + self.window_seconds - time.time()))

    def _recent_locked(self, key: str) -> list[float]:
        cutoff = time.time() - self.window_seconds
        recent = [value for value in self._attempts.get(key, []) if value > cutoff]
        if recent:
            self._attempts[key] = recent
        else:
            self._attempts.pop(key, None)
        return recent
