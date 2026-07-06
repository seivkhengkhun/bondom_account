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
    /v1/api/create-order.php          → phone + order_id (charges balance;
                                         failed rents auto-refund)
    /v1/api/check-otp.php?order_id=   → SMS code, or refunded/expired

check-otp / create-order response field names are parsed defensively —
the docs show shapes but production fields were not observable without
spending money; ``_extract_*`` helpers accept the likely variants.
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
from shared.models import SmsOrder, SmsOrderStatus
from shared.services import (
    InsufficientBalanceError,  # noqa: F401  (re-exported for callers)
    add_user_balance,
    spend_user_balance,
    transaction_scope,
)

logger = logging.getLogger(__name__)

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


def sell_price(cost: Decimal) -> Decimal:
    """Customer price = provider cost + fixed markup."""
    return _money(Decimal(str(cost)) + settings.sms_markup_usd)


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
                    "price": sell_price(cost),
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
        if value and str(value).lower() not in ("none", "null", "waiting"):
            return str(value)
    message = str(data.get("message", ""))
    match = re.search(r"\b(\d{4,8})\b", message)
    if match and "code" in message.lower():
        return match.group(1)
    return ""


def _is_refunded(data: dict) -> bool:
    blob = " ".join(
        str(data.get(k, "")) for k in ("status", "message", "state", "error")
    ).lower()
    return any(
        word in blob
        for word in ("refund", "expired", "cancel", "timeout", "failed")
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
    price = offer["price"]

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
        failed = (
            not phone
            or not provider_order_id
            or str(data.get("status", "success")).lower()
            in ("error", "failed", "fail")
            or "rent failed" in str(data.get("message", "")).lower()
        )
        if failed:
            raise SmsServiceError(
                str(data.get("message") or "Number rental failed — "
                    "provider had no stock. You were not charged.")
            )
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
        data = await _get(
            "/v1/api/check-otp.php", order_id=order.provider_order_id
        )
    except SmsServiceError:
        return order  # transient — stay in waiting, page keeps polling

    otp = _extract_otp(data)
    refunded = not otp and _is_refunded(data)

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
