"""Telegram Login Widget verification + signed session cookies.

The website has no passwords: customers sign in with the official
Telegram Login Widget, which redirects back with user fields plus an
HMAC ``hash`` computed by Telegram using the bot token. Verifying that
hash proves the login came from Telegram for OUR bot — and gives us the
same ``telegram_id`` identity the bot uses, so wallet/orders are shared.

Sessions are stateless signed cookies (HMAC-SHA256 keyed off the bot
token); no session table needed.
"""

import base64
import hashlib
import hmac
import json
import time

from shared.config import settings

SESSION_COOKIE = "bondom_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
LOGIN_MAX_AGE_SECONDS = 24 * 3600  # reject stale login callbacks


def _session_key() -> bytes:
    return hashlib.sha256(f"session:{settings.bot_token}".encode()).digest()


def sign_session(telegram_id: int, username: str) -> str:
    payload = {
        "tid": telegram_id,
        "u": username,
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    raw = (
        base64.urlsafe_b64encode(json.dumps(payload).encode())
        .decode()
        .rstrip("=")
    )
    sig = hmac.new(_session_key(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def read_session(cookie: str | None) -> dict | None:
    """Return {'tid': int, 'u': str} for a valid unexpired cookie."""
    if not cookie or "." not in cookie:
        return None
    raw, sig = cookie.rsplit(".", 1)
    expected = hmac.new(
        _session_key(), raw.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(
            base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        )
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return None
    if not isinstance(data.get("tid"), int):
        return None
    return data


def verify_telegram_login(params: dict[str, str]) -> dict[str, str] | None:
    """Validate a Telegram Login Widget callback (returns fields or None).

    Per https://core.telegram.org/widgets/login#checking-authorization:
    secret_key = SHA256(bot_token); hash must equal HMAC-SHA256 of the
    sorted ``key=value`` lines of every other field.
    """
    fields = dict(params)
    their_hash = fields.pop("hash", None)
    if not their_hash:
        return None

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hashlib.sha256(settings.bot_token.encode()).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, their_hash):
        return None

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        return None
    if time.time() - auth_date > LOGIN_MAX_AGE_SECONDS:
        return None
    return fields
