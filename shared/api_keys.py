"""API key issuance and verification for the public API.

Keys look like ``bk_live_<prefix>_<secret>``. Only a SHA-256 hash of the
whole key is stored, so the plaintext exists exactly once — in the
response that created it. A database leak therefore does not hand over
working credentials.

SHA-256 rather than PBKDF2 is deliberate here: unlike a password, the key
is 32 bytes of ``secrets.token_urlsafe`` entropy, so there is nothing to
brute force, and a per-request key derivation would add real latency to
every API call.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import ApiKey, ApiRequestLog
from shared.services import transaction_scope

KEY_ENV = "live"
KEY_PREFIX = f"bk_{KEY_ENV}_"
PREFIX_BYTES = 4
SECRET_BYTES = 24

MAX_KEYS_PER_USER = 5
DEFAULT_RATE_LIMIT_PER_MIN = 60

# Scope -> what it unlocks. Enforced by the API dependency.
SCOPES = {
    "read": "Read products, stock and your own account",
    "orders": "Create orders and pay them from your wallet",
    "sms": "Rent SMS numbers and read their codes",
}
DEFAULT_SCOPES = "read"


@dataclass(frozen=True)
class IssuedKey:
    """Returned once, at creation. ``plaintext`` is never recoverable."""

    id: int
    name: str
    prefix: str
    plaintext: str
    scopes: str


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str]:
    """Return ``(plaintext, prefix)`` for a fresh key."""
    prefix = secrets.token_hex(PREFIX_BYTES)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return f"{KEY_PREFIX}{prefix}_{secret}", prefix


def normalise_scopes(scopes: list[str] | str | None) -> str:
    if scopes is None:
        return DEFAULT_SCOPES
    if isinstance(scopes, str):
        scopes = [s.strip() for s in scopes.split(",")]
    kept = [s for s in scopes if s in SCOPES]
    return ",".join(dict.fromkeys(kept)) or DEFAULT_SCOPES


async def count_active_keys(session: AsyncSession, user_id: int) -> int:
    async with transaction_scope(session):
        return await session.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.user_id == user_id, ApiKey.is_active.is_(True))
        ) or 0


async def create_key(
    session: AsyncSession,
    user_id: int,
    name: str = "",
    scopes: list[str] | str | None = None,
) -> IssuedKey:
    """Issue a new key. The plaintext is returned once and never stored."""
    plaintext, prefix = generate_key()
    row = ApiKey(
        user_id=user_id,
        name=(name or "Untitled key")[:64],
        prefix=prefix,
        key_hash=hash_key(plaintext),
        scopes=normalise_scopes(scopes),
        rate_limit_per_min=DEFAULT_RATE_LIMIT_PER_MIN,
    )
    async with transaction_scope(session):
        session.add(row)
        await session.flush()

    return IssuedKey(
        id=row.id,
        name=row.name,
        prefix=prefix,
        plaintext=plaintext,
        scopes=row.scopes,
    )


async def resolve_key(session: AsyncSession, plaintext: str) -> ApiKey | None:
    """Look up an active key by its plaintext, or ``None``.

    Matching is on the hash, so the lookup is a single indexed equality
    check and the plaintext never has to be compared in the database.
    """
    if not plaintext or not plaintext.startswith(KEY_PREFIX):
        return None
    async with transaction_scope(session):
        return await session.scalar(
            select(ApiKey).where(
                ApiKey.key_hash == hash_key(plaintext),
                ApiKey.is_active.is_(True),
            )
        )


async def list_keys(session: AsyncSession, user_id: int) -> list[ApiKey]:
    async with transaction_scope(session):
        return list(
            (
                await session.scalars(
                    select(ApiKey)
                    .where(ApiKey.user_id == user_id)
                    .order_by(ApiKey.id.desc())
                )
            ).all()
        )


async def get_key(
    session: AsyncSession, user_id: int, key_id: int
) -> ApiKey | None:
    """Fetch one key, scoped to its owner so ids cannot be guessed."""
    return await session.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
    )


async def revoke_key(session: AsyncSession, user_id: int, key_id: int) -> bool:
    """Deactivate a key. Returns False if it is not the caller's.

    Commits explicitly rather than relying on ``transaction_scope``:
    revoking a leaked credential must be durable the moment it returns
    True. If this joined a caller-owned transaction that was later rolled
    back, the key would keep working while the UI reported success.
    """
    row = await get_key(session, user_id, key_id)
    if row is None or not row.is_active:
        return False
    row.is_active = False
    row.revoked_at = datetime.now(timezone.utc)
    await session.commit()
    return True


async def touch_key(session: AsyncSession, key_id: int) -> None:
    """Record usage. Best-effort; callers ignore failures."""
    async with transaction_scope(session):
        row = await session.get(ApiKey, key_id)
        if row is not None:
            row.last_used_at = datetime.now(timezone.utc)
            row.request_count = (row.request_count or 0) + 1


async def log_request(
    session: AsyncSession,
    *,
    api_key_id: int | None,
    user_id: int | None,
    method: str,
    path: str,
    status_code: int,
    duration_ms: int,
    ip: str = "",
) -> None:
    async with transaction_scope(session):
        session.add(
            ApiRequestLog(
                api_key_id=api_key_id,
                user_id=user_id,
                method=method[:8],
                path=path[:255],
                status_code=status_code,
                duration_ms=duration_ms,
                ip=ip[:64],
            )
        )


async def recent_requests(
    session: AsyncSession, user_id: int, limit: int = 100
) -> list[ApiRequestLog]:
    return list(
        (
            await session.scalars(
                select(ApiRequestLog)
                .where(ApiRequestLog.user_id == user_id)
                .order_by(ApiRequestLog.id.desc())
                .limit(limit)
            )
        ).all()
    )


async def usage_summary(session: AsyncSession, user_id: int) -> dict:
    """Totals for the developer dashboard."""
    total = await session.scalar(
        select(func.count())
        .select_from(ApiRequestLog)
        .where(ApiRequestLog.user_id == user_id)
    )
    errors = await session.scalar(
        select(func.count())
        .select_from(ApiRequestLog)
        .where(
            ApiRequestLog.user_id == user_id,
            ApiRequestLog.status_code >= 400,
        )
    )
    avg_ms = await session.scalar(
        select(func.avg(ApiRequestLog.duration_ms)).where(
            ApiRequestLog.user_id == user_id
        )
    )
    return {
        "total": total or 0,
        "errors": errors or 0,
        "avg_ms": int(avg_ms or 0),
    }
