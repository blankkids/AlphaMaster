"""Local username/password authentication for the AlphaMaster Web UI."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

SESSION_COOKIE = "alphamaster_session"
DEFAULT_USERNAME = "admin"
DEFAULT_SESSION_HOURS = 12
MAX_SESSION_HOURS = 24 * 30


def auth_username() -> str:
    return os.getenv("WEB_AUTH_USERNAME", DEFAULT_USERNAME).strip()


def auth_password() -> str:
    return os.getenv("WEB_AUTH_PASSWORD", "")


def auth_configured() -> bool:
    return bool(auth_username() and auth_password().strip())


def authenticate(username: str, password: str) -> bool:
    """Compare both fields in constant time without accepting an empty config."""
    expected_username = auth_username()
    expected_password = auth_password()
    if not expected_username or not expected_password:
        return False
    username_ok = hmac.compare_digest(str(username), expected_username)
    password_ok = hmac.compare_digest(str(password), expected_password)
    return username_ok and password_ok


def session_max_age() -> int:
    raw_value = os.getenv("WEB_AUTH_SESSION_HOURS", str(DEFAULT_SESSION_HOURS))
    try:
        hours = int(raw_value)
    except (TypeError, ValueError):
        hours = DEFAULT_SESSION_HOURS
    hours = min(max(hours, 1), MAX_SESSION_HOURS)
    return hours * 60 * 60


def cookie_secure() -> bool:
    value = os.getenv("WEB_AUTH_COOKIE_SECURE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _signing_key() -> bytes:
    secret = os.getenv("WEB_AUTH_SECRET", "").strip() or auth_password()
    return hashlib.sha256(f"AlphaMaster:web-auth:{secret}".encode("utf-8")).digest()


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_session_token(username: str, *, now: int | None = None) -> str:
    issued_at = int(time.time() if now is None else now)
    payload: dict[str, Any] = {
        "u": username,
        "iat": issued_at,
        "exp": issued_at + session_max_age(),
    }
    encoded_payload = _encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    )
    signature = hmac.new(
        _signing_key(),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_encode(signature)}"


def verify_session_token(token: str | None, *, now: int | None = None) -> str | None:
    if not token or not auth_configured():
        return None
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        expected_signature = _encode(
            hmac.new(
                _signing_key(),
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(encoded_signature, expected_signature):
            return None
        payload = json.loads(_decode(encoded_payload))
        username = str(payload["u"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    current_time = int(time.time() if now is None else now)
    if expires_at <= current_time or username != auth_username():
        return None
    return username
