"""Bakong KHQR payment service.

Responsibilities:
  - Generate a dynamic KHQR (QR string + md5 lookup hash) for an order.
  - Persist the payment session (md5, amount, expiry) in the ``payments``
    table so every Bakong transaction can be traced back to its order.
  - Verify payment status against the Bakong API, gracefully handling
    timeouts.
  - Poll in the background (plain asyncio) until the payment is confirmed
    or the QR expires.

Dev mode: with ``PAYMENT_DEV_MODE=true`` QR generation and verification
are simulated so the full purchase flow works without a Bakong merchant
account.

Production note on polling: ``poll_payment_until_paid`` is an in-process
asyncio task — simple, but it dies with the process. If you need
durability across restarts (or run multiple API workers), move the poll
loop to a task queue with retries (Celery beat / arq / APScheduler) keyed
by ``payment.id``, using the same ``verify_payment`` + ``confirm_payment``
functions below.
"""

import asyncio
import hashlib
import io
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.database import AsyncSessionLocal
from shared.models import Order, OrderStatus, Payment, PaymentStatus
from shared.services import (
    OrderNotFoundError,
    cancel_order_and_release_inventory,
    transaction_scope,
)

logger = logging.getLogger(__name__)

QR_LIFETIME = timedelta(seconds=900)  # 15 minutes
VERIFY_TIMEOUT_SECONDS = 15
POLL_INTERVAL_SECONDS = 10


class PaymentError(Exception):
    """Raised when a payment operation cannot be performed."""


def _khqr():
    """Build a bakong-khqr client (lazy import so dev mode needs no lib)."""
    try:
        from bakong_khqr import KHQR
    except ImportError as exc:  # pragma: no cover
        raise PaymentError(
            "The 'bakong-khqr' package is required for real KHQR flows: "
            "pip install bakong-khqr"
        ) from exc
    if not settings.bakong_token or not settings.bakong_account_id:
        raise PaymentError(
            "BAKONG_TOKEN and BAKONG_ACCOUNT_ID must be set when "
            "PAYMENT_DEV_MODE is false"
        )
    return KHQR(settings.bakong_token)


# --------------------------------------------------------------------------- #
# 1. QR generation
# --------------------------------------------------------------------------- #
async def generate_payment_qr(
    order_id: str, amount: float, currency: str = "USD"
) -> tuple[str, str]:
    """Generate a dynamic KHQR for an order.

    Returns ``(qr_string, md5_hash)`` — the QR string is displayed to the
    customer, the md5 is Bakong's key for verifying the transaction later.
    """
    if settings.payment_dev_mode:
        qr_string = f"DEV-KHQR|order={order_id}|amount={amount}|cur={currency}"
        return qr_string, hashlib.md5(qr_string.encode()).hexdigest()

    def _build() -> tuple[str, str]:
        khqr = _khqr()
        qr_string = khqr.create_qr(
            bank_account=settings.bakong_account_id,
            merchant_name=settings.merchant_name,
            merchant_city=settings.merchant_city,
            amount=amount,
            currency=currency,
            store_label=settings.merchant_name,
            phone_number="",
            bill_number=f"ORDER-{order_id}",
            terminal_label="digital-store",
        )
        md5_hash = khqr.generate_md5(qr_string)
        return qr_string, md5_hash

    # bakong-khqr is synchronous (requests-based) — run it off the loop.
    return await asyncio.to_thread(_build)


def render_qr_png(qr_string: str) -> bytes:
    """Render a KHQR payload string into a scannable PNG (bytes).

    Banking apps scan the image, not the raw EMV string — so the bot sends
    this picture rather than the text payload.
    """
    import qrcode

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_string)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 2. Verification (graceful on timeouts)
# --------------------------------------------------------------------------- #
async def verify_payment(md5_hash: str) -> bool:
    """Return True if Bakong reports the transaction as PAID.

    Timeouts and transport errors are treated as "not paid yet" (logged,
    never raised) so a flaky network can't crash a poll loop or a bot
    handler — the next attempt simply retries.
    """
    if settings.payment_dev_mode:
        logger.info("PAYMENT_DEV_MODE: auto-approving payment md5=%s", md5_hash)
        return True

    def _check() -> str:
        return _khqr().check_payment(md5_hash)  # -> "PAID" | "UNPAID"

    try:
        status = await asyncio.wait_for(
            asyncio.to_thread(_check), timeout=VERIFY_TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning("Bakong verification timed out for md5=%s", md5_hash)
        return False
    except PaymentError:
        raise  # misconfiguration — surface it, don't swallow
    except Exception:
        logger.exception("Bakong verification failed for md5=%s", md5_hash)
        return False

    return status == "PAID"


# --------------------------------------------------------------------------- #
# 3. Database integration
# --------------------------------------------------------------------------- #
async def create_payment_session(session: AsyncSession, order_id: int) -> Payment:
    """Generate a KHQR for the order and persist the md5 immediately.

    The QR is generated *outside* the insert transaction so the DB is
    never blocked on a network call.
    """
    async with transaction_scope(session):
        order = await session.get(Order, order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        if order.status is not OrderStatus.PENDING:
            raise PaymentError(f"Order {order_id} is already {order.status.value}")
        amount = order.total_price

    qr_string, md5_hash = await generate_payment_qr(
        str(order_id), float(amount), "USD"
    )

    payment = Payment(
        order_id=order_id,
        md5=md5_hash,
        qr_string=qr_string,
        amount=amount,
        currency="USD",
        status=PaymentStatus.PENDING,
        expires_at=datetime.now(timezone.utc) + QR_LIFETIME,
    )
    async with transaction_scope(session):
        session.add(payment)
    return payment


async def get_latest_payment(
    session: AsyncSession, order_id: int
) -> Payment | None:
    async with transaction_scope(session):
        return await session.scalar(
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.created_at.desc(), Payment.id.desc())
            .limit(1)
        )


async def confirm_payment(session: AsyncSession, payment_id: int) -> Payment:
    """Mark a payment (and its order) as paid — one atomic transaction."""
    async with transaction_scope(session):
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise PaymentError(f"Payment {payment_id} not found")
        payment.status = PaymentStatus.PAID
        order = await session.get(Order, payment.order_id)
        if order is not None and order.status is OrderStatus.PENDING:
            order.status = OrderStatus.PAID
    return payment


def _is_expired(payment: Payment) -> bool:
    expires_at = payment.expires_at
    if expires_at.tzinfo is None:  # SQLite returns naive datetimes
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at


def is_payment_expired(payment: Payment) -> bool:
    """Public helper for callers that need expiry checks."""
    return _is_expired(payment)


# --------------------------------------------------------------------------- #
# 4. Background polling
# --------------------------------------------------------------------------- #
async def poll_payment_until_paid(
    payment_id: int, interval: int = POLL_INTERVAL_SECONDS
) -> bool:
    """Poll Bakong every ``interval`` seconds until PAID or QR expiry.

    Owns its DB sessions (safe to run as ``asyncio.create_task``). Returns
    True if the payment was confirmed, False if it expired.
    """
    while True:
        async with AsyncSessionLocal() as session:
            payment = await session.get(Payment, payment_id)
        if payment is None:
            logger.error("poll: payment %s vanished", payment_id)
            return False
        if payment.status is PaymentStatus.PAID:
            return True

        if _is_expired(payment):
            async with AsyncSessionLocal() as session:
                async with transaction_scope(session):
                    stale = await session.get(Payment, payment_id)
                    if stale is not None and stale.status is PaymentStatus.PENDING:
                        stale.status = PaymentStatus.EXPIRED
                        await cancel_order_and_release_inventory(
                            session, stale.order_id
                        )
            logger.info("poll: payment %s expired unpaid", payment_id)
            return False

        if await verify_payment(payment.md5):
            async with AsyncSessionLocal() as session:
                await confirm_payment(session, payment_id)
            logger.info("poll: payment %s confirmed", payment_id)
            return True

        await asyncio.sleep(interval)
