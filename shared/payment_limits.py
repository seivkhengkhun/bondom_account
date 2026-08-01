"""Rate limiting for payment-session creation.

Every KHQR request costs real work: a call out to Bakong, a database row,
and a background poller that lives for the QR's 15-minute lifetime. A
customer refreshing a checkout page, or a script hammering it, can spawn
those faster than they retire.

The limiter is intentionally in-process and per-user. That matches how
the payment pollers already work (``poll_payment_until_paid`` is an
asyncio task in this process), so it adds no new infrastructure. If the
app is ever run as multiple workers, move this to the database or Redis
alongside the pollers — see the note in ``payment_service``.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

# Per user, per kind. Deliberately generous: a real customer creating a
# handful of payments in a minute is normal, a script making dozens is
# not.
MAX_REQUESTS = 5
WINDOW_SECONDS = 60

# Refuse a second request for the same target within this many seconds.
# Catches the common case of a double-submit or an impatient refresh,
# which is what produces most duplicate payment rows.
DUPLICATE_COOLDOWN_SECONDS = 10

_hits: dict[tuple[str, int], deque[float]] = {}
_last_target: dict[tuple[str, int], tuple[str, float]] = {}


class PaymentRateLimited(Exception):
    """Raised when a caller is creating payment requests too quickly."""

    def __init__(self, retry_after: int, reason: str) -> None:
        self.retry_after = max(1, int(retry_after))
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class LimitStatus:
    allowed: bool
    remaining: int
    retry_after: int


def _prune(bucket: deque[float], now: float) -> None:
    while bucket and now - bucket[0] >= WINDOW_SECONDS:
        bucket.popleft()


def check(kind: str, user_id: int, target: str = "") -> LimitStatus:
    """Read-only view of the limit, for showing state without consuming it."""
    now = time.monotonic()
    bucket = _hits.setdefault((kind, user_id), deque())
    _prune(bucket, now)
    remaining = max(0, MAX_REQUESTS - len(bucket))
    retry_after = 0
    if not remaining and bucket:
        retry_after = int(WINDOW_SECONDS - (now - bucket[0])) + 1
    return LimitStatus(bool(remaining), remaining, retry_after)


def consume(kind: str, user_id: int, target: str = "") -> None:
    """Record a payment request, or raise :class:`PaymentRateLimited`.

    ``kind`` separates the buckets (``order`` vs ``topup``) so a burst of
    top-ups cannot lock someone out of paying for an order. ``target``
    identifies what is being paid for, enabling the duplicate check.
    """
    now = time.monotonic()
    key = (kind, user_id)

    if target:
        previous = _last_target.get(key)
        if previous and previous[0] == target:
            elapsed = now - previous[1]
            if elapsed < DUPLICATE_COOLDOWN_SECONDS:
                raise PaymentRateLimited(
                    DUPLICATE_COOLDOWN_SECONDS - elapsed,
                    "You just created that payment request. Please use the "
                    "existing QR, or wait a moment before trying again.",
                )

    bucket = _hits.setdefault(key, deque())
    _prune(bucket, now)
    if len(bucket) >= MAX_REQUESTS:
        retry_after = int(WINDOW_SECONDS - (now - bucket[0])) + 1
        raise PaymentRateLimited(
            retry_after,
            f"Too many payment requests. Please wait {retry_after} seconds "
            "before creating another.",
        )

    bucket.append(now)
    if target:
        _last_target[key] = (target, now)


def reset(kind: str | None = None, user_id: int | None = None) -> None:
    """Clear counters. Used by tests and after an admin intervention."""
    if kind is None and user_id is None:
        _hits.clear()
        _last_target.clear()
        return
    for store in (_hits, _last_target):
        for key in [
            k
            for k in store
            if (kind is None or k[0] == kind)
            and (user_id is None or k[1] == user_id)
        ]:
            store.pop(key, None)
