"""SMS activation via the Angkor Phone SMS reseller API (website-only).

Money flow per rental (all Decimal, never float):

    customer wallet  −(provider cost + SMS_MARKUP_USD)   ← charged FIRST
    provider call    create-order (charges OUR reseller balance)
    on failure       instant wallet refund, order marked failed
    on "no SMS"      provider auto-refunds us → we credit the customer

The provider API is plain GET with ``?key=`` auth:
    /v1/api/user                      → {"balance": "0.22", "id": "..."}
    /v1/stock?category=fb             → {"status": "success", "countries": [
                                          {"country","countryCode","flag",
                                           "price": 0.03, "stock": null}]}
    /v1/api/create-order.php?category=&country=
        → {"phone","order_id","status":"running","amount":0.03}
          or {"status":"error","message":"Out of stock"}
    /v1/api/check-otp.php?id=<order_id>   ← NOTE: param is ``id`` not
        ``order_id`` (the published docs are wrong). Returns
        {"status":"running","otp":"","counter":0} while waiting, and the
        code in ``otp`` once received. Verified live 2026-07-06.

Response shapes above were confirmed by real create-order/check-otp
calls; ``_extract_*`` helpers still accept common variants defensively.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.database import AsyncSessionLocal
from shared.models import AppSetting, SmsOrder, SmsOrderStatus
from shared.services import (
    InsufficientBalanceError,  # noqa: F401  (re-exported for callers)
    add_user_balance,
    spend_user_balance,
    transaction_scope,
)

logger = logging.getLogger(__name__)

SMS_MARKUP_KEY = "sms_markup_usd"

CATEGORIES = {"facebook": "fb", "instagram": "ig"}
MAX_WAITING_PER_USER = 3
POLL_THROTTLE_SECONDS = 3
WATCH_INTERVAL_SECONDS = 5
WATCH_TIMEOUT_SECONDS = 20 * 60  # give up watching after 20 minutes
STOCK_CACHE_SECONDS = 60
REQUEST_TIMEOUT = 15

_stock_cache: dict[str, tuple[float, list[dict]]] = {}
_watch_tasks: set[asyncio.Task] = set()


class SmsServiceError(Exception):
    """User-visible SMS feature failure."""


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# In-process cache of the DB markup so hot paths (stock listing) don't hit
# the DB per row; refreshed on set and every MARKUP_CACHE_SECONDS.
_markup_cache: tuple[float, Decimal] | None = None
MARKUP_CACHE_SECONDS = 30


async def get_markup(session: AsyncSession | None = None) -> Decimal:
    """Current profit markup (USD). DB value overrides the .env default."""
    global _markup_cache
    if _markup_cache and time.monotonic() - _markup_cache[0] < MARKUP_CACHE_SECONDS:
        return _markup_cache[1]

    async def _read(s: AsyncSession) -> Decimal:
        async with transaction_scope(s):
            row = await s.get(AppSetting, SMS_MARKUP_KEY)
        if row is None:
            return _money(settings.sms_markup_usd)
        try:
            return _money(Decimal(row.value))
        except Exception:
            return _money(settings.sms_markup_usd)

    if session is not None:
        value = await _read(session)
    else:
        async with AsyncSessionLocal() as own:
            value = await _read(own)
    _markup_cache = (time.monotonic(), value)
    return value


async def set_markup(session: AsyncSession, markup: Decimal) -> Decimal:
    """Persist a new markup (admin tool). Returns the stored value."""
    global _markup_cache
    value = _money(markup)
    if value < 0:
        raise SmsServiceError("Markup cannot be negative")
    async with transaction_scope(session):
        row = await session.get(AppSetting, SMS_MARKUP_KEY)
        if row is None:
            session.add(AppSetting(key=SMS_MARKUP_KEY, value=str(value)))
        else:
            row.value = str(value)
    _markup_cache = (time.monotonic(), value)
    return value


def sell_price(cost, markup: Decimal) -> Decimal:
    """Customer price = provider cost + markup."""
    return _money(Decimal(str(cost)) + markup)


async def _get(path: str, **params) -> dict:
    if not settings.sms_api_key:
        raise SmsServiceError("SMS service is not configured")
    params["key"] = settings.sms_api_key
    url = f"{settings.sms_api_base}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        logger.warning("SMS provider unreachable (%s): %s", path, exc)
        raise SmsServiceError(
            "SMS provider is unreachable — please try again shortly"
        ) from exc
    try:
        data = response.json()
    except ValueError:
        logger.warning(
            "SMS provider non-JSON (%s %s): %.200s",
            path, response.status_code, response.text,
        )
        raise SmsServiceError("SMS provider returned an invalid response")
    if not isinstance(data, dict):
        raise SmsServiceError("SMS provider returned an invalid response")
    logger.info("SMS API %s -> %s %.300s", path, response.status_code, data)
    return data


# --------------------------------------------------------------------------- #
# Stock / catalog
# --------------------------------------------------------------------------- #
async def get_stock(category: str, fresh: bool = False) -> list[dict]:
    """Country offers for a category, with customer (marked-up) prices."""
    category = category.lower()
    if category not in CATEGORIES:
        raise SmsServiceError(f"Unknown category: {category}")

    cached = _stock_cache.get(category)
    if cached and not fresh and time.monotonic() - cached[0] < STOCK_CACHE_SECONDS:
        return cached[1]

    data = await _get("/v1/stock", category=CATEGORIES[category])
    countries = data.get("countries")
    if not isinstance(countries, list):
        raise SmsServiceError("SMS provider returned no stock data")

    markup = await get_markup()
    offers = []
    for entry in countries:
        try:
            cost = Decimal(str(entry["price"]))
            offers.append(
                {
                    "country": str(entry.get("country", "")),
                    "code": str(entry.get("countryCode", "")).upper(),
                    "flag": str(entry.get("flag", "")),
                    "cost": cost,
                    "price": sell_price(cost, markup),
                    "stock": entry.get("stock"),
                }
            )
        except (KeyError, ArithmeticError, TypeError):
            logger.warning("SMS stock entry skipped: %r", entry)
    _stock_cache[category] = (time.monotonic(), offers)
    return offers


async def get_offer(category: str, country_code: str) -> dict:
    offers = await get_stock(category)
    country_code = country_code.upper()
    offer = next((o for o in offers if o["code"] == country_code), None)
    if offer is None:
        raise SmsServiceError(
            f"{category.title()} numbers for {country_code} are not available"
        )
    return offer


# --------------------------------------------------------------------------- #
# Defensive response parsing
# --------------------------------------------------------------------------- #
def _extract_phone(data: dict) -> str:
    for key in ("phone", "number", "phone_number", "phoneNumber"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def _extract_order_id(data: dict) -> str:
    for key in ("order_id", "orderId", "id", "orderID"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def _extract_otp(data: dict) -> str:
    for key in ("otp", "code", "sms", "otp_code", "otpCode", "sms_code"):
        value = data.get(key)
        if value and str(value).strip().lower() not in (
            "none", "null", "waiting", ""
        ):
            return str(value).strip()
    return ""


# check-otp status values: "running" = still waiting; anything terminal
# without an otp (refunded/expired/cancelled/timeout) means the money
# came back to our reseller balance.
_WAITING_STATES = {"running", "waiting", "pending", "active", "ok"}


def _is_waiting(data: dict) -> bool:
    return str(data.get("status", "")).strip().lower() in _WAITING_STATES


def _is_refunded(data: dict) -> bool:
    blob = " ".join(
        str(data.get(k, "")) for k in ("status", "message", "state", "error")
    ).lower()
    return any(
        word in blob
        for word in (
            "refund", "expired", "cancel", "timeout", "failed", "error"
        )
    )


# --------------------------------------------------------------------------- #
# Order lifecycle
# --------------------------------------------------------------------------- #
async def create_sms_order(
    session: AsyncSession, user_id: int, category: str, country_code: str
) -> SmsOrder:
    """Charge the wallet, rent a number, persist the order.

    Raises InsufficientBalanceError (wallet too low) or SmsServiceError
    (validation/provider failure — wallet already refunded).
    """
    if not settings.sms_enabled:
        raise SmsServiceError("SMS activation is currently disabled")
    category = category.lower()
    offer = await get_offer(category, country_code)
    # Recompute at purchase time with the CURRENT markup (the cached offer
    # price could be up to a minute stale after an admin markup change).
    markup = await get_markup(session)
    price = sell_price(offer["cost"], markup)

    async with transaction_scope(session):
        waiting = await session.scalar(
            select(func.count(SmsOrder.id)).where(
                SmsOrder.user_id == user_id,
                SmsOrder.status == SmsOrderStatus.WAITING,
            )
        )
    if int(waiting or 0) >= MAX_WAITING_PER_USER:
        raise SmsServiceError(
            f"You already have {waiting} numbers waiting for SMS — "
            "finish or wait for those first"
        )

    # 1. Charge the customer FIRST (atomic; raises if insufficient).
    await spend_user_balance(session, user_id, price)

    # 2. Rent the number; ANY failure refunds the wallet immediately.
    try:
        data = await _get(
            "/v1/api/create-order.php",
            category=CATEGORIES[category],
            country=country_code.lower(),
        )
        phone = _extract_phone(data)
        provider_order_id = _extract_order_id(data)
        status = str(data.get("status", "")).strip().lower()
        # Real success looks like {"phone","order_id","status":"running",
        # "amount":0.03}; failures are {"status":"error","message":"..."}.
        failed = (
            not phone
            or not provider_order_id
            or status in ("error", "failed", "fail")
        )
        if failed:
            message = str(data.get("message") or "").strip()
            raise SmsServiceError(
                (f"Number rental failed: {message}. You were not charged."
                 if message
                 else "Number rental failed — no stock. You were not charged.")
            )
        # Prefer the provider's actually-charged amount as our true cost.
        try:
            if data.get("amount") is not None:
                offer = {**offer, "cost": Decimal(str(data["amount"]))}
        except (ArithmeticError, TypeError):
            pass
    except Exception as exc:
        await add_user_balance(session, user_id, price)
        logger.warning("SMS rent failed, wallet refunded: %s", exc)
        if isinstance(exc, SmsServiceError):
            raise
        raise SmsServiceError(
            "Number rental failed — you were not charged"
        ) from exc

    order = SmsOrder(
        user_id=user_id,
        category=category,
        country=offer["country"],
        country_code=offer["code"],
        phone=phone,
        provider_order_id=provider_order_id,
        cost=offer["cost"],
        price=price,
        status=SmsOrderStatus.WAITING,
    )
    async with transaction_scope(session):
        session.add(order)
    logger.info(
        "SMS order %s: user=%s %s/%s phone=%s price=%s",
        order.id, user_id, category, offer["code"], phone, price,
    )
    return order


async def refresh_sms_order(session: AsyncSession, order_id: int) -> SmsOrder:
    """Poll the provider for an order's SMS code; handle refunds.

    Idempotent: terminal orders return unchanged; the provider is called
    at most once per POLL_THROTTLE_SECONDS per order.
    """
    async with transaction_scope(session):
        order = await session.get(SmsOrder, order_id)
    if order is None:
        raise SmsServiceError(f"SMS order {order_id} not found")
    if order.status is not SmsOrderStatus.WAITING:
        return order

    now = datetime.now(timezone.utc)
    last = order.last_checked_at
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < POLL_THROTTLE_SECONDS:
            return order

    try:
        # The live API requires ``id`` (NOT ``order_id``, despite the docs).
        data = await _get("/v1/api/check-otp.php", id=order.provider_order_id)
    except SmsServiceError:
        return order  # transient — stay in waiting, page keeps polling

    otp = _extract_otp(data)
    # Terminal-without-code = provider refunded us. While "running", wait.
    refunded = not otp and not _is_waiting(data) and _is_refunded(data)

    async with transaction_scope(session):
        fresh = await session.get(SmsOrder, order_id)
        if fresh is None or fresh.status is not SmsOrderStatus.WAITING:
            return fresh or order
        fresh.last_checked_at = now
        if otp:
            fresh.status = SmsOrderStatus.COMPLETED
            fresh.otp_code = otp
            logger.info("SMS order %s completed (code received)", order_id)
        elif refunded:
            fresh.status = SmsOrderStatus.REFUNDED
            logger.info("SMS order %s refunded by provider", order_id)
        order = fresh

    if order.status is SmsOrderStatus.REFUNDED:
        # Outside the row transaction: credit the customer back in full.
        await add_user_balance(session, order.user_id, order.price)
        logger.info(
            "SMS order %s: refunded $%s to user %s wallet",
            order_id, order.price, order.user_id,
        )
    return order


async def watch_sms_order(order_id: int) -> None:
    """Background poller so refunds land even if the buyer closes the tab."""
    deadline = time.monotonic() + WATCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(WATCH_INTERVAL_SECONDS)
        try:
            async with AsyncSessionLocal() as session:
                order = await refresh_sms_order(session, order_id)
        except SmsServiceError:
            return
        if order is None or order.status is not SmsOrderStatus.WAITING:
            return
    logger.warning("SMS order %s watch timed out (still waiting)", order_id)


def spawn_sms_watcher(order_id: int) -> None:
    task = asyncio.create_task(watch_sms_order(order_id))
    _watch_tasks.add(task)
    task.add_done_callback(_watch_tasks.discard)


async def list_user_sms_orders(
    session: AsyncSession, user_id: int, limit: int = 20
) -> list[SmsOrder]:
    async with transaction_scope(session):
        result = await session.scalars(
            select(SmsOrder)
            .where(SmsOrder.user_id == user_id)
            .order_by(SmsOrder.created_at.desc(), SmsOrder.id.desc())
            .limit(limit)
        )
        return list(result.all())


# --------------------------------------------------------------------------- #
# Admin reporting
# --------------------------------------------------------------------------- #
async def sms_stats(session: AsyncSession) -> dict:
    """Aggregate SMS economics for the admin dashboard.

    Revenue/cost/profit count only COMPLETED orders (refunded/failed
    orders returned the customer's money, so they net to zero).
    """
    async with transaction_scope(session):
        completed = (await session.execute(
            select(
                func.count(SmsOrder.id),
                func.coalesce(func.sum(SmsOrder.price), 0),
                func.coalesce(func.sum(SmsOrder.cost), 0),
            ).where(SmsOrder.status == SmsOrderStatus.COMPLETED)
        )).one()
        waiting = await session.scalar(
            select(func.count(SmsOrder.id)).where(
                SmsOrder.status == SmsOrderStatus.WAITING
            )
        )
        refunded = await session.scalar(
            select(func.count(SmsOrder.id)).where(
                SmsOrder.status == SmsOrderStatus.REFUNDED
            )
        )
    count, revenue, cost = completed
    revenue = Decimal(str(revenue))
    cost = Decimal(str(cost))
    return {
        "completed": int(count or 0),
        "waiting": int(waiting or 0),
        "refunded": int(refunded or 0),
        "revenue": _money(revenue),
        "cost": _money(cost),
        "profit": _money(revenue - cost),
    }


async def list_sms_orders(
    session: AsyncSession, limit: int = 200
) -> list[SmsOrder]:
    """All SMS orders (newest first) for the admin history table."""
    async with transaction_scope(session):
        result = await session.scalars(
            select(SmsOrder)
            .order_by(SmsOrder.created_at.desc(), SmsOrder.id.desc())
            .limit(limit)
        )
        return list(result.all())
