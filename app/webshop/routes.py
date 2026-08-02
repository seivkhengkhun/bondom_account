"""Customer web storefront — same shared services as the Telegram bot.

Every page reads/writes the same database as the bot, so catalog, stock,
wallets and orders are always in sync. Customers authenticate with the
Telegram Login Widget (see auth.py), which maps them onto the exact same
User row the bot uses. Purchases spawn the bot's own payment watcher, so
paid orders are ALSO delivered into the buyer's Telegram chat.
"""

import asyncio
import base64
import logging
from pathlib import Path

from aiogram import Bot
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from shared import payment_limits, payment_service, services
from shared.config import settings
from shared.database import AsyncSessionLocal
from shared.models import OrderStatus, Product

from .auth import (
    CSRF_FIELD,
    check_csrf,
    csrf_token,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    read_session,
    sign_session,
    verify_telegram_login,
)

logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_STATIC_DIR = Path(__file__).parent / "static"


def _asset_version() -> str:
    """Short fingerprint of the stylesheet, appended to its URL.

    Without this the browser keeps serving whatever ``shop.css`` it cached
    before the deploy, so markup ships styled by the previous release —
    which is how a redesigned component ends up rendering as raw text.
    Recomputed per call but keyed on mtime, so it is a stat() not a read.
    """
    css = _STATIC_DIR / "shop.css"
    try:
        stat = css.stat()
    except OSError:
        return "0"
    return f"{int(stat.st_mtime)}-{stat.st_size}"

_background_tasks: set[asyncio.Task] = set()
_bot: Bot | None = None
_bot_username: str = ""


def _get_bot() -> Bot:
    """Process-lifetime Bot client for web-triggered Telegram deliveries."""
    global _bot
    if _bot is None:
        _bot = Bot(settings.bot_token)
    return _bot


async def _get_bot_username() -> str:
    global _bot_username
    if not _bot_username:
        me = await _get_bot().get_me()
        _bot_username = me.username or ""
    return _bot_username


def _auth_url(request: Request) -> str:
    host = request.headers.get("host", "localhost")
    scheme = (
        "http"
        if host.startswith(("127.", "localhost", "0.0.0.0"))
        else "https"
    )
    return f"{scheme}://{host}/auth/telegram"


def _current_session(request: Request) -> dict | None:
    return read_session(request.cookies.get(SESSION_COOKIE))


async def _render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("session_user", _current_session(request))
    context.setdefault("bot_username", await _get_bot_username())
    context.setdefault("auth_url", _auth_url(request))
    context.setdefault("sms_enabled", settings.sms_enabled)
    context.setdefault("csrf_token", csrf_token(_current_session(request)))
    context.setdefault("merchant", settings.merchant_name)
    context.setdefault("asset_v", _asset_version())
    return templates.TemplateResponse(
        request=request, name=template, context=context
    )


_ERROR_COPY: dict[int, tuple[str, str]] = {
    404: (
        "Page not found",
        "That link does not lead anywhere. The product may have been "
        "removed, or the address may be mistyped.",
    ),
    403: (
        "Not available",
        "You do not have access to this page.",
    ),
    429: (
        "Too many requests",
        "You have made a lot of requests in a short time. Wait a moment "
        "and try again.",
    ),
    500: (
        "Something went wrong",
        "That is on us, not on you. Nothing was charged. Please try again "
        "in a moment.",
    ),
}

# Fallbacks for codes with no specific copy. A 4xx is the request's fault
# and a 5xx is ours, so they must not share the 500 wording — telling
# someone "nothing was charged" on an unrelated 4xx is a false promise.
_ERROR_FALLBACK: dict[int, tuple[str, str]] = {
    4: ("Something isn't right", "We could not complete that request."),
    5: (
        "Service unavailable",
        "Something on our side is not responding. Please try again shortly.",
    ),
}


async def render_error(request: Request, code: int) -> HTMLResponse:
    """Branded HTML error page. Presentation only — no business logic.

    Used by the app-level handler in ``app.api.main`` for browser requests;
    API clients keep the JSON envelope. Deliberately tolerant: an error
    page that raises while rendering is worse than a plain one, so the
    bot-username lookup (a Telegram round-trip on first call) is allowed
    to fail.
    """
    heading, message = _ERROR_COPY.get(
        code, _ERROR_FALLBACK.get(code // 100, _ERROR_FALLBACK[5])
    )
    try:
        bot_username = await _get_bot_username()
    except Exception:  # noqa: BLE001 — never let the error page itself fail
        bot_username = ""
    response = await _render(
        request,
        "error.html",
        code=code,
        heading=heading,
        message=message,
        bot_username=bot_username,
    )
    response.status_code = code
    return response


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@router.get("/auth/telegram")
async def auth_telegram(request: Request) -> RedirectResponse:
    fields = verify_telegram_login(dict(request.query_params))
    if fields is None:
        return RedirectResponse("/?login=failed")

    telegram_id = int(fields["id"])
    username = fields.get("username") or fields.get("first_name") or ""
    async with AsyncSessionLocal() as session:
        await services.get_or_create_user(session, telegram_id, username)

    response = RedirectResponse(request.query_params.get("next") or "/")
    host = request.headers.get("host", "")
    is_local = host.startswith(("127.", "localhost", "0.0.0.0"))
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(telegram_id, username),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        # Never let the session cookie travel over plaintext in production.
        secure=not is_local,
    )
    return response


@router.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/")
    response.delete_cookie(SESSION_COOKIE)
    return response


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
async def _catalog() -> tuple[list, bool]:
    async with AsyncSessionLocal() as session:
        overviews = await services.list_product_overviews(session)
        show_stock = await services.get_bot_show_stock(session)
    return [o for o in overviews if o.product.is_active], show_stock


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    active, show_stock = await _catalog()
    grouped: dict[str, list] = {}
    for o in active:
        grouped.setdefault(o.product.category or "Other", []).append(o)
    return await _render(
        request,
        "home.html",
        grouped=grouped,
        show_stock=show_stock,
        login_failed=request.query_params.get("login") == "failed",
        error=request.query_params.get("error", ""),
    )


@router.get("/p/{product_id}", response_class=HTMLResponse)
async def product_page(request: Request, product_id: int) -> HTMLResponse:
    active, show_stock = await _catalog()
    selected = next((o for o in active if o.product.id == product_id), None)
    if selected is None:
        return RedirectResponse("/")  # type: ignore[return-value]
    async with AsyncSessionLocal() as session:
        note = await services.get_product_client_note(session, product_id)
        min_qty = await services.get_product_min_quantity(session, product_id)
    return await _render(
        request,
        "product.html",
        o=selected,
        note=note,
        min_qty=min_qty,
        show_stock=show_stock,
        error=request.query_params.get("error", ""),
    )


# --------------------------------------------------------------------------- #
# Purchase flow
# --------------------------------------------------------------------------- #
@router.post("/web/buy")
async def web_buy(
    request: Request,
    product_id: int = Form(...),
    quantity: int = Form(1),
    csrf_token: str = Form(""),
):
    sess = _current_session(request)
    if sess is None:
        return RedirectResponse(f"/p/{product_id}", status_code=303)
    if not check_csrf(sess, csrf_token):
        return RedirectResponse(
            f"/p/{product_id}?error=" + quote_plus(
                "Your session expired. Please try again."
            ),
            status_code=303,
        )
    quantity = max(1, min(quantity, 100))

    from shared.schemas import OrderCreate

    try:
        async with AsyncSessionLocal() as session:
            user = await services.get_or_create_user(
                session, sess["tid"], sess.get("u") or ""
            )

            # Check the throttle BEFORE allocating stock. Rejecting after
            # the order exists would leave a pending order holding
            # inventory that nobody can pay for — worse than the traffic
            # the limit is meant to prevent.
            status = payment_limits.check("order", user.id)
            if not status.allowed:
                return RedirectResponse(
                    f"/p/{product_id}?error="
                    + quote_plus(
                        "Too many payment requests. Please wait "
                        f"{status.retry_after} seconds before trying again."
                    ),
                    status_code=303,
                )

            order = await services.create_order_and_allocate_stock(
                session,
                OrderCreate(
                    user_id=user.id, product_id=product_id, quantity=quantity
                ),
            )
            try:
                payment = await payment_service.create_payment_session(
                    session, order.id
                )
            except Exception:
                # Never strand an order holding stock: release it and let
                # the caller see the original failure.
                await services.cancel_order_and_release_inventory(
                    session, order.id
                )
                raise
    except services.OutOfStockError:
        return RedirectResponse(
            f"/p/{product_id}?error=Not+enough+stock+available",
            status_code=303,
        )
    except (
        services.UserPermanentlyBlockedError,
        services.UserInactiveError,
    ):
        return RedirectResponse(
            f"/p/{product_id}?error=Your+account+is+blocked",
            status_code=303,
        )
    except services.BelowMinimumQuantityError as exc:
        return RedirectResponse(
            f"/p/{product_id}?error="
            + quote_plus(
                f"This product has a minimum order of {exc.minimum}."
            ),
            status_code=303,
        )
    except payment_limits.PaymentRateLimited as exc:
        return RedirectResponse(
            f"/p/{product_id}?error=" + quote_plus(str(exc)),
            status_code=303,
        )
    except services.ProductNotFoundError:
        return RedirectResponse("/?error=Product+unavailable", status_code=303)

    # Reuse the bot's watcher: auto-confirms, marks delivered, and sends
    # the items to the buyer's Telegram chat when payment arrives.
    from app.bot.handlers import _watch_payment_and_auto_deliver

    task = asyncio.create_task(
        _watch_payment_and_auto_deliver(
            _get_bot(), sess["tid"], order.id, payment.id
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return RedirectResponse(f"/pay/{order.id}", status_code=303)


async def _owned_order(request: Request, order_id: int):
    """Return (order, user) if the order belongs to the session user."""
    sess = _current_session(request)
    if sess is None:
        return None, None
    async with AsyncSessionLocal() as session:
        user = await services.get_user_by_telegram_id(session, sess["tid"])
        if user is None:
            return None, None
        try:
            order = await services.get_order_with_items(session, order_id)
        except services.OrderNotFoundError:
            return None, user
        if order.user_id != user.id:
            return None, user
    return order, user


@router.get("/pay/{order_id}", response_class=HTMLResponse)
async def pay_page(request: Request, order_id: int):
    order, _ = await _owned_order(request, order_id)
    if order is None:
        return RedirectResponse("/")
    if order.status in (OrderStatus.DELIVERED, OrderStatus.PAID):
        return RedirectResponse(f"/order/{order_id}")
    if order.status is OrderStatus.CANCELED:
        return RedirectResponse("/?error=Order+was+canceled")

    async with AsyncSessionLocal() as session:
        payment = await payment_service.get_latest_payment(session, order_id)
    if payment is None:
        return RedirectResponse("/")

    qr_b64 = base64.b64encode(
        payment_service.render_qr_png(payment.qr_string)
    ).decode()
    expires_at = payment.expires_at.isoformat() + "Z"
    return await _render(
        request,
        "pay.html",
        order=order,
        amount=f"{payment.amount:.2f}",
        qr_b64=qr_b64,
        expires_at=expires_at,
    )


@router.get("/web/status/{order_id}")
async def order_status(request: Request, order_id: int) -> JSONResponse:
    order, _ = await _owned_order(request, order_id)
    if order is None:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return JSONResponse({"status": order.status.value})


@router.get("/order/{order_id}", response_class=HTMLResponse)
async def order_page(request: Request, order_id: int):
    order, _ = await _owned_order(request, order_id)
    if order is None:
        return RedirectResponse("/")

    grouped: dict[int, list[str]] = {}
    for item in order.items:
        grouped.setdefault(item.product_id, []).append(item.data)

    sections = []
    async with AsyncSessionLocal() as session:
        for pid, values in grouped.items():
            product = await session.get(Product, pid)
            note = await services.get_product_client_note(session, pid)
            sections.append(
                {
                    "name": product.name if product else f"Product {pid}",
                    "warranty": product.warranty_days if product else 0,
                    "note": note,
                    "items": values,
                }
            )
    return await _render(
        request, "order.html", order=order, sections=sections
    )


@router.get("/my/orders", response_class=HTMLResponse)
async def my_orders(request: Request):
    sess = _current_session(request)
    if sess is None:
        return RedirectResponse("/")
    async with AsyncSessionLocal() as session:
        user = await services.get_user_by_telegram_id(session, sess["tid"])
        orders = (
            await services.list_user_orders(session, user.id, limit=20)
            if user
            else []
        )
        balance = (
            await services.get_user_balance(session, user.id)
            if user
            else 0
        )
    return await _render(
        request,
        "orders.html",
        orders=orders,
        balance=f"{balance:.2f}",
    )


# --------------------------------------------------------------------------- #
# Wallet top-up (KHQR) — needed to fund SMS activations on the web
# --------------------------------------------------------------------------- #
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, quote_plus

from shared.models import TopupStatus
from shared import sms_service


async def _session_user(request: Request):
    sess = _current_session(request)
    if sess is None:
        return None
    async with AsyncSessionLocal() as session:
        return await services.get_or_create_user(
            session, sess["tid"], sess.get("u") or ""
        )


@router.get("/wallet", response_class=HTMLResponse)
async def wallet_page(request: Request):
    user = await _session_user(request)
    if user is None:
        return RedirectResponse("/")
    async with AsyncSessionLocal() as session:
        balance = await services.get_user_balance(session, user.id)
    async with AsyncSessionLocal() as session:
        override = await services.has_topup_override(session, user.id)
    min_topup = payment_service.effective_min_topup(override)
    # Whole dollars normally, cents when an admin has lowered the floor.
    min_topup_label = (
        f"{min_topup:.2f}" if override else f"{int(min_topup)}"
    )
    return await _render(
        request,
        "wallet.html",
        balance=f"{balance:.2f}",
        topup_override=override,
        min_topup_label=min_topup_label,
        error=request.query_params.get("error", ""),
        success=request.query_params.get("success", ""),
        # Limits are rendered from the service constants so the form and
        # the server check can never disagree.
        min_topup=f"{min_topup:.2f}",
        max_topup=int(payment_service.MAX_TOPUP),
        presets=[p for p in (2, 5, 10, 20) if p >= min_topup],
    )


@router.post("/web/wallet/topup")
async def wallet_topup(
    request: Request, amount: str = Form(...), csrf_token: str = Form("")
):
    user = await _session_user(request)
    if user is None:
        return RedirectResponse("/", status_code=303)
    if not check_csrf(_current_session(request), csrf_token):
        return RedirectResponse(
            "/wallet?error=" + quote_plus("Your session expired. Please try again."),
            status_code=303,
        )
    try:
        value = Decimal(amount.strip().replace(",", "")).quantize(
            Decimal("0.01")
        )
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            "/wallet?error=" + quote_plus("Enter a valid amount."),
            status_code=303,
        )

    # Server-side limit check — the browser form is a convenience, this is
    # what actually holds if the POST is crafted by hand.
    async with AsyncSessionLocal() as session:
        override = await services.has_topup_override(session, user.id)
    reason = payment_service.topup_amount_error(value, override=override)
    if reason is not None:
        return RedirectResponse(
            "/wallet?error=" + quote_plus(reason), status_code=303
        )

    async with AsyncSessionLocal() as session:
        try:
            topup = await payment_service.create_wallet_topup_session(
                session, user.id, value
            )
        except payment_limits.PaymentRateLimited as exc:
            return RedirectResponse(
                "/wallet?error=" + quote_plus(str(exc)), status_code=303
            )
        except payment_service.PaymentError as exc:
            return RedirectResponse(
                "/wallet?error=" + quote_plus(str(exc)), status_code=303
            )
    task = asyncio.create_task(
        payment_service.poll_wallet_topup_until_paid(topup.id)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return RedirectResponse(f"/wallet/pay/{topup.id}", status_code=303)


@router.get("/wallet/pay/{topup_id}", response_class=HTMLResponse)
async def wallet_pay_page(request: Request, topup_id: int):
    user = await _session_user(request)
    if user is None:
        return RedirectResponse("/")
    async with AsyncSessionLocal() as session:
        topup = await payment_service.get_wallet_topup(session, topup_id)
    if topup is None or topup.user_id != user.id:
        return RedirectResponse("/wallet")
    if topup.status is TopupStatus.PAID:
        return RedirectResponse("/wallet?success=Top-up+received")

    qr_b64 = base64.b64encode(
        payment_service.render_qr_png(topup.qr_string)
    ).decode()
    return await _render(
        request,
        "wallet_pay.html",
        topup=topup,
        amount=f"{topup.amount:.2f}",
        qr_b64=qr_b64,
        expires_at=topup.expires_at.isoformat() + "Z",
    )


@router.get("/web/wallet/status/{topup_id}")
async def wallet_topup_status(request: Request, topup_id: int) -> JSONResponse:
    user = await _session_user(request)
    if user is None:
        return JSONResponse({"status": "unknown"}, status_code=404)
    async with AsyncSessionLocal() as session:
        topup = await payment_service.get_wallet_topup(session, topup_id)
    if topup is None or topup.user_id != user.id:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return JSONResponse({"status": topup.status.value})


# --------------------------------------------------------------------------- #
# SMS activation (website-only — intentionally absent from the bot)
# --------------------------------------------------------------------------- #
@router.get("/sms", response_class=HTMLResponse)
async def sms_page(request: Request):
    if not settings.sms_enabled:
        return RedirectResponse("/")
    user = await _session_user(request)
    balance = Decimal("0")
    if user is not None:
        async with AsyncSessionLocal() as session:
            balance = await services.get_user_balance(session, user.id)

    stock: dict[str, list] = {}
    stock_error = ""
    for category in sms_service.CATEGORIES:
        try:
            # Ranked best-delivering first, with recent success-rate badges.
            stock[category] = await sms_service.get_stock_ranked(category)
        except sms_service.SmsServiceError as exc:
            stock[category] = []
            stock_error = str(exc)

    return await _render(
        request,
        "sms.html",
        stock=stock,
        balance=balance,
        balance_str=f"{balance:.2f}",
        stock_error=stock_error,
        error=request.query_params.get("error", ""),
    )


@router.post("/web/sms/buy")
async def sms_buy(
    request: Request,
    category: str = Form(...),
    country: str = Form(...),
    csrf_token: str = Form(""),
):
    if not check_csrf(_current_session(request), csrf_token):
        return RedirectResponse(
            "/sms?error=" + quote_plus("Your session expired. Please try again."),
            status_code=303,
        )
    if not settings.sms_enabled:
        return RedirectResponse("/", status_code=303)
    user = await _session_user(request)
    if user is None:
        return RedirectResponse("/sms", status_code=303)

    try:
        async with AsyncSessionLocal() as session:
            order = await sms_service.create_sms_order(
                session, user.id, category, country
            )
    except sms_service.InsufficientBalanceError as exc:
        return RedirectResponse(
            "/wallet?error=" + quote(
                f"Not enough balance: need ${exc.required:.2f}, "
                f"you have ${exc.balance:.2f}. Top up below."
            ),
            status_code=303,
        )
    except sms_service.SmsServiceError as exc:
        return RedirectResponse(
            "/sms?error=" + quote(str(exc)), status_code=303
        )

    sms_service.spawn_sms_watcher(order.id)
    return RedirectResponse(f"/sms/{order.id}", status_code=303)


async def _owned_sms_order(request: Request, sms_id: int):
    user = await _session_user(request)
    if user is None:
        return None
    async with AsyncSessionLocal() as session:
        order = await session.get(sms_service.SmsOrder, sms_id)
    if order is None or order.user_id != user.id:
        return None
    return order


def _sms_deadline_iso(order) -> str:
    from datetime import timedelta, timezone

    created = order.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    deadline = created + timedelta(seconds=sms_service.SMS_ORDER_TTL_SECONDS)
    return deadline.isoformat()


def _sms_txn_ref(order) -> str:
    return f"SMS-{order.id:06d}"


@router.get("/sms/{sms_id}", response_class=HTMLResponse)
async def sms_order_page(request: Request, sms_id: int):
    order = await _owned_sms_order(request, sms_id)
    if order is None:
        return RedirectResponse("/sms")
    # Lazily settle on view too, so a refund shows even without JS polling.
    try:
        async with AsyncSessionLocal() as session:
            order = await sms_service.refresh_sms_order(session, order.id)
    except sms_service.SmsServiceError:
        pass
    resolved_at = (
        order.last_checked_at.strftime("%Y-%m-%d %H:%M")
        if order.last_checked_at else ""
    )
    return await _render(
        request,
        "sms_order.html",
        o=order,
        deadline_iso=_sms_deadline_iso(order),
        txn_ref=_sms_txn_ref(order),
        resolved_at=resolved_at,
    )


@router.get("/web/sms/{sms_id}/status")
async def sms_order_status(request: Request, sms_id: int) -> JSONResponse:
    order = await _owned_sms_order(request, sms_id)
    if order is None:
        return JSONResponse({"status": "unknown"}, status_code=404)
    try:
        async with AsyncSessionLocal() as session:
            order = await sms_service.refresh_sms_order(session, order.id)
    except sms_service.SmsServiceError:
        pass
    resolved_at = (
        order.last_checked_at.strftime("%Y-%m-%d %H:%M")
        if order.last_checked_at else None
    )
    return JSONResponse(
        {
            "status": order.status.value,
            "otp": order.otp_code or None,
            "price": f"{order.price:.2f}",
            "txn_ref": _sms_txn_ref(order),
            "resolved_at": resolved_at,
        }
    )


@router.get("/my/sms", response_class=HTMLResponse)
async def my_sms_orders(request: Request):
    if not settings.sms_enabled:
        return RedirectResponse("/")
    user = await _session_user(request)
    if user is None:
        return RedirectResponse("/sms")
    async with AsyncSessionLocal() as session:
        orders = await sms_service.list_user_sms_orders(session, user.id)
        balance = await services.get_user_balance(session, user.id)
    return await _render(
        request,
        "sms_orders.html",
        orders=orders,
        balance=f"{balance:.2f}",
    )
