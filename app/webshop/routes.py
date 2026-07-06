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

from shared import payment_service, services
from shared.config import settings
from shared.database import AsyncSessionLocal
from shared.models import OrderStatus, Product

from .auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    read_session,
    sign_session,
    verify_telegram_login,
)

logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

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
    return templates.TemplateResponse(
        request=request, name=template, context=context
    )


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
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(telegram_id, username),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
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
    return await _render(
        request,
        "product.html",
        o=selected,
        note=note,
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
):
    sess = _current_session(request)
    if sess is None:
        return RedirectResponse(f"/p/{product_id}", status_code=303)
    quantity = max(1, min(quantity, 100))

    from shared.schemas import OrderCreate

    try:
        async with AsyncSessionLocal() as session:
            user = await services.get_or_create_user(
                session, sess["tid"], sess.get("u") or ""
            )
            order = await services.create_order_and_allocate_stock(
                session,
                OrderCreate(
                    user_id=user.id, product_id=product_id, quantity=quantity
                ),
            )
            payment = await payment_service.create_payment_session(
                session, order.id
            )
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
