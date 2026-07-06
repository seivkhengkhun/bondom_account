"""aiogram 3.x handlers — thin Telegram layer over shared/services.py.

Every handler opens its own short-lived session from ``AsyncSessionLocal``
(the same factory FastAPI uses) and delegates all business rules to the
shared service layer, so bot and API can never disagree about behavior.
"""

import html
import asyncio
import logging
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy.ext.asyncio import AsyncSession

from shared import payment_service, services
from shared.database import AsyncSessionLocal
from shared.models import Order, OrderStatus, Product
from shared.schemas import OrderCreate

logger = logging.getLogger(__name__)
router = Router()
_background_tasks: set[asyncio.Task[object]] = set()


def _track_background_task(task: asyncio.Task[object], label: str) -> None:
    _background_tasks.add(task)

    def _done(done_task: asyncio.Task[object]) -> None:
        _background_tasks.discard(done_task)
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.info("Background task canceled: %s", label)
        except Exception:
            logger.exception("Background task failed: %s", label)

    task.add_done_callback(_done)


async def _verify_payment_or_alert(
    callback: CallbackQuery, md5: str
) -> bool | None:
    """Verify with Bakong; on misconfiguration alert the user and return None.

    PaymentError here means the check itself cannot run (expired token,
    blocked IP) — showing "not detected yet" would mislead a customer who
    actually paid.
    """
    try:
        return await payment_service.verify_payment(md5)
    except payment_service.PaymentError:
        logger.exception("Bakong verification is misconfigured")
        await callback.answer(
            "⚠️ Payment system error on our side — your money is safe. "
            "Please contact support.",
            show_alert=True,
        )
        return None


class PurchaseState(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_topup_amount = State()


BTN_BROWSE = "🛒 Browse Products"
BTN_TOPUP = "💳 Top Up Wallet"
BTN_BALANCE = "💰 My Balance"
BTN_ORDERS = "📦 My Orders"


def _main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BROWSE), KeyboardButton(text=BTN_TOPUP)],
            [KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_ORDERS)],
        ],
        resize_keyboard=True,
    )


async def _deny_if_permanently_blocked(
    session: AsyncSession, telegram_id: int
) -> bool:
    user = await services.get_user_by_telegram_id(session, telegram_id)
    if user is None:
        return False
    return await services.is_user_blocked(session, user.id)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.from_user is None:
        return
    async with AsyncSessionLocal() as session:
        user = await services.get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        if await services.is_user_blocked(session, user.id):
            await message.answer(
                "🚫 Your account has been permanently blocked. Contact support."
            )
            return
    await message.answer(
        "សូមស្វាគមន៍មកកាន់ Bondom Account - បណ្តុំអាខោន!\n"
        "យើងផ្តល់ជូននូវសេវាកម្ម និងគណនីចម្រុះជាច្រើនប្រភេទ។ "
        "សូមរីករាយជាមួយបទពិសោធន៍ដ៏ល្អឥតខ្ចោះជាមួយយើង។\n\n"
        "Welcome to Bondom Account!\n"
        "We provide a variety of high-quality accounts and services. "
        "We are pleased to have you with us and hope you enjoy our services.\n\n"
        f"Use {BTN_BROWSE} to start shopping.",
        reply_markup=_main_menu(),
    )


# --------------------------------------------------------------------------- #
# Product catalog — categories → paginated product list → product card.
# Navigation edits one message in place so the chat stays clean.
# --------------------------------------------------------------------------- #
CATALOG_PAGE_SIZE = 8


async def _active_overviews() -> tuple[list, bool]:
    async with AsyncSessionLocal() as session:
        overviews = await services.list_product_overviews(session)
        show_stock = await services.get_bot_show_stock(session)
    return [o for o in overviews if o.product.is_active], show_stock


def _catalog_categories(active: list) -> list[str]:
    return sorted({(o.product.category or "Other") for o in active})


def _category_menu(active: list) -> tuple[str, InlineKeyboardMarkup]:
    categories = _catalog_categories(active)
    counts = {
        c: sum(1 for o in active if (o.product.category or "Other") == c)
        for c in categories
    }
    rows = [
        [
            InlineKeyboardButton(
                text=f"📂 {c}  ({counts[c]})",
                callback_data=f"pcat:{i}:0",
            )
        ]
        for i, c in enumerate(categories)
    ]
    text = (
        "🛍 <b>Product Catalog</b>\n\n"
        "Choose a category to see its products:"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _category_page(
    active: list, show_stock: bool, cat_idx: int, page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    categories = _catalog_categories(active)
    if not 0 <= cat_idx < len(categories):
        return None
    category = categories[cat_idx]
    items = [
        o for o in active if (o.product.category or "Other") == category
    ]
    pages = max(1, -(-len(items) // CATALOG_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * CATALOG_PAGE_SIZE:(page + 1) * CATALOG_PAGE_SIZE]

    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{o.product.name} — ${o.product.price}"
                    + (f"  ({o.available} left)" if show_stock else "")
                ),
                callback_data=f"pview:{o.product.id}:{cat_idx}:{page}",
            )
        ]
        for o in chunk
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️ Prev", callback_data=f"pcat:{cat_idx}:{page - 1}"
            )
        )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="Next ▶️", callback_data=f"pcat:{cat_idx}:{page + 1}"
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text="📂 All categories", callback_data="pcats")]
    )
    page_info = f" — page {page + 1}/{pages}" if pages > 1 else ""
    text = (
        f"📂 <b>{html.escape(category)}</b>{page_info}\n\n"
        "Tap a product to see details and buy:"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _product_card(
    active: list, show_stock: bool, product_id: int, cat_idx: int, page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    selected = next(
        (o for o in active if o.product.id == product_id), None
    )
    if selected is None:
        return None
    product = selected.product
    async with AsyncSessionLocal() as session:
        note = await services.get_product_client_note(session, product.id)

    lines = [f"📦 <b>{html.escape(product.name)}</b>", ""]
    lines.append(f"💵 Price: <b>${product.price}</b>")
    if product.warranty_days:
        lines.append(f"🛡 Warranty: {product.warranty_days} days")
    if show_stock:
        lines.append(f"📦 Stock: {selected.available} left")
    if note:
        lines.append(f"📝 {html.escape(note)}")
    lines.extend(["", "Choose how to buy:"])

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Buy with KHQR",
                    callback_data=f"buy:{product.id}",
                ),
                InlineKeyboardButton(
                    text="⚡ Buy 1 (Wallet)",
                    callback_data=f"wb1:{product.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Back",
                    callback_data=f"pcat:{cat_idx}:{page}",
                )
            ],
        ]
    )
    return "\n".join(lines), keyboard


async def _edit_or_answer(callback: CallbackQuery, text: str, markup) -> None:
    """Edit the catalog message in place; ignore 'not modified' noise."""
    assert callback.message is not None
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


async def _show_products_for_user(message: Message, telegram_id: int) -> None:
    async with AsyncSessionLocal() as session:
        if await _deny_if_permanently_blocked(session, telegram_id):
            await message.answer(
                "🚫 Your account has been permanently blocked. Contact support."
            )
            return

    active, show_stock = await _active_overviews()
    if not active:
        await message.answer("No products are available right now.")
        return

    categories = _catalog_categories(active)
    if len(categories) == 1:
        # Single category — skip the menu, show its products directly.
        rendered = _category_page(active, show_stock, 0, 0)
        if rendered is not None:
            text, markup = rendered
            await message.answer(text, reply_markup=markup)
        return

    text, markup = _category_menu(active)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "pcats")
async def cb_catalog_menu(callback: CallbackQuery) -> None:
    active, _ = await _active_overviews()
    if not active:
        await callback.answer("No products are available right now.", show_alert=True)
        return
    text, markup = _category_menu(active)
    await _edit_or_answer(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("pcat:"))
async def cb_catalog_category(callback: CallbackQuery) -> None:
    if callback.data is None:
        return
    try:
        _, cat_idx, page = callback.data.split(":")
        rendered_args = int(cat_idx), int(page)
    except ValueError:
        await callback.answer()
        return
    active, show_stock = await _active_overviews()
    rendered = _category_page(active, show_stock, *rendered_args) if active else None
    if rendered is None:
        await callback.answer("Category changed — reopening catalog.")
        if active:
            text, markup = _category_menu(active)
            await _edit_or_answer(callback, text, markup)
        return
    text, markup = rendered
    await _edit_or_answer(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("pview:"))
async def cb_catalog_product(callback: CallbackQuery) -> None:
    if callback.data is None:
        return
    try:
        _, product_id, cat_idx, page = callback.data.split(":")
        args = int(product_id), int(cat_idx), int(page)
    except ValueError:
        await callback.answer()
        return
    active, show_stock = await _active_overviews()
    rendered = (
        await _product_card(active, show_stock, *args) if active else None
    )
    if rendered is None:
        await callback.answer(
            "This product is no longer available.", show_alert=True
        )
        return
    text, markup = rendered
    await _edit_or_answer(callback, text, markup)
    await callback.answer()


@router.message(Command("products"))
async def cmd_products(message: Message) -> None:
    if message.from_user is None:
        return
    await _show_products_for_user(message, message.from_user.id)


@router.message(F.text == BTN_BROWSE)
async def btn_browse(message: Message) -> None:
    if message.from_user is None:
        return
    await _show_products_for_user(message, message.from_user.id)


_ORDER_STATUS_EMOJI = {
    OrderStatus.PENDING: "⏳",
    OrderStatus.PAID: "💰",
    OrderStatus.DELIVERED: "✅",
    OrderStatus.CANCELED: "❌",
}


async def _show_orders_for_user(message: Message, telegram_id: int) -> None:
    async with AsyncSessionLocal() as session:
        user = await services.get_user_by_telegram_id(session, telegram_id)
        if user is not None and await services.is_user_blocked(session, user.id):
            await message.answer(
                "🚫 Your account has been permanently blocked. Contact support."
            )
            return
        orders = (
            await services.list_user_orders(session, user.id, limit=5)
            if user is not None
            else []
        )

    if not orders:
        await message.answer(
            f"You have no orders yet — tap {BTN_BROWSE} to start shopping."
        )
        return

    lines = ["📦 <b>Your recent orders</b>", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for order in orders:
        emoji = _ORDER_STATUS_EMOJI.get(order.status, "•")
        lines.append(
            f"{emoji} Order <b>#{order.id}</b> — ${order.total_price} — "
            f"{order.status.value} — {order.created_at:%Y-%m-%d %H:%M}"
        )
        if order.status is OrderStatus.DELIVERED:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"📥 Resend items of order #{order.id}",
                        callback_data=f"rsnd:{order.id}",
                    )
                ]
            )

    await message.answer(
        "\n".join(lines),
        reply_markup=(
            InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        ),
    )


@router.message(Command("myorders"))
async def cmd_my_orders(message: Message) -> None:
    if message.from_user is None:
        return
    await _show_orders_for_user(message, message.from_user.id)


@router.message(F.text == BTN_ORDERS)
async def btn_my_orders(message: Message) -> None:
    if message.from_user is None:
        return
    await _show_orders_for_user(message, message.from_user.id)


@router.callback_query(F.data.startswith("rsnd:"))
async def cb_resend_order(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    if callback.from_user is None:
        return
    order_id = int(callback.data.split(":", 1)[1])

    async with AsyncSessionLocal() as session:
        user = await services.get_user_by_telegram_id(
            session, callback.from_user.id
        )
        try:
            order = await services.get_order_with_items(session, order_id)
        except services.OrderNotFoundError:
            await callback.answer("Order not found.", show_alert=True)
            return
        if user is None or order.user_id != user.id:
            await callback.answer(
                "This order does not belong to you.", show_alert=True
            )
            return
        if order.status is not OrderStatus.DELIVERED:
            await callback.answer(
                "Only delivered orders can be resent.", show_alert=True
            )
            return

    await _deliver_order_to_chat(
        callback.message.bot,
        callback.message.chat.id,
        order,
        title="📥 Resent items",
    )
    await callback.answer("Items sent again ⬆")


@router.message(F.text == BTN_BALANCE)
async def btn_balance(message: Message) -> None:
    if message.from_user is None:
        return
    async with AsyncSessionLocal() as session:
        user = await services.get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        if await services.is_user_blocked(session, user.id):
            await message.answer(
                "🚫 Your account has been permanently blocked. Contact support."
            )
            return
        balance = await services.get_user_balance(session, user.id)
    await message.answer(f"💰 Your wallet balance: <b>${balance:.2f}</b>")


@router.message(F.text == BTN_TOPUP)
async def btn_topup(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    async with AsyncSessionLocal() as session:
        user = await services.get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        if await services.is_user_blocked(session, user.id):
            await message.answer(
                "🚫 Your account has been permanently blocked. Contact support."
            )
            return
    await state.set_state(PurchaseState.waiting_for_topup_amount)
    await message.answer("Enter top-up amount in USD (example: 10 or 15.50)")


@router.message(PurchaseState.waiting_for_topup_amount)
async def msg_topup_amount(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        await state.clear()
        return

    text = (message.text or "").strip().replace(",", "")
    try:
        amount = Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await message.answer("Please enter a valid amount, e.g. 10 or 15.50")
        return

    if amount <= 0:
        await message.answer("Amount must be greater than 0")
        return

    async with AsyncSessionLocal() as session:
        user = await services.get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        try:
            topup = await payment_service.create_wallet_topup_session(
                session, user.id, amount
            )
        except services.UserPermanentlyBlockedError:
            await message.answer(
                "🚫 Your account has been permanently blocked. Contact support."
            )
            await state.clear()
            return

    await state.clear()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ I've paid top-up — check",
                    callback_data=f"tchk:{topup.id}",
                )
            ]
        ]
    )
    photo = BufferedInputFile(
        payment_service.render_qr_png(topup.qr_string),
        filename=f"topup_{topup.id}.png",
    )
    await message.answer_photo(
        photo=photo,
        caption=(
            f"💳 Wallet top-up <b>#{topup.id}</b>\n"
            f"Amount: <b>${topup.amount}</b>\n\n"
            "Scan and pay, then tap check."
        ),
        reply_markup=kb,
    )

    _track_background_task(
        asyncio.create_task(payment_service.poll_wallet_topup_until_paid(topup.id)),
        f"wallet-topup:{topup.id}",
    )


@router.callback_query(F.data.startswith("tchk:"))
async def cb_check_topup(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None:
        return
    topup_id = int(callback.data.split(":", 1)[1])

    async with AsyncSessionLocal() as session:
        user = await services.get_or_create_user(
            session, callback.from_user.id, callback.from_user.username
        )
        if await services.is_user_blocked(session, user.id):
            await callback.answer("🚫 Permanently blocked account.", show_alert=True)
            return

        topup = await payment_service.get_wallet_topup(session, topup_id)
        if topup is None or topup.user_id != user.id:
            await callback.answer("Top-up not found.", show_alert=True)
            return

        if topup.status.value == "paid":
            balance = await services.get_user_balance(session, user.id)
            await callback.answer(
                f"Wallet credited. Balance: ${balance:.2f}", show_alert=True
            )
            return

        if payment_service.is_wallet_topup_expired(topup):
            await callback.answer("Top-up session expired.", show_alert=True)
            return

        paid = await _verify_payment_or_alert(callback, topup.md5)
        if paid is None:
            return
        if not paid:
            await callback.answer("Payment not detected yet.", show_alert=True)
            return

        await payment_service.confirm_wallet_topup(session, topup.id)
        balance = await services.get_user_balance(session, user.id)

    await callback.answer(
        f"✅ Top-up successful. New balance: ${balance:.2f}", show_alert=True
    )


@router.callback_query(F.data.startswith("wb1:"))
async def cb_wallet_buy_one(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None:
        return
    product_id = int(callback.data.split(":", 1)[1])

    async with AsyncSessionLocal() as session:
        user = await services.get_or_create_user(
            session, callback.from_user.id, callback.from_user.username
        )
        try:
            order = await services.buy_one_with_wallet(session, user.id, product_id)
        except services.UserPermanentlyBlockedError:
            await callback.answer("🚫 Permanently blocked account.", show_alert=True)
            return
        except services.UserInactiveError:
            await callback.answer("🚫 Suspended account.", show_alert=True)
            return
        except services.InsufficientBalanceError as exc:
            await callback.answer(
                (
                    f"Insufficient balance. Need ${exc.required:.2f}, "
                    f"have ${exc.balance:.2f}."
                ),
                show_alert=True,
            )
            return
        except services.OutOfStockError:
            await callback.answer("Out of stock.", show_alert=True)
            return
        except services.ProductNotFoundError:
            await callback.answer("Product not available.", show_alert=True)
            return

        await services.mark_order_delivered(session, order.id)
        order = await services.get_order_with_items(session, order.id)
        balance = await services.get_user_balance(session, user.id)

    await _deliver_order_to_chat(callback.message.bot, callback.message.chat.id, order)
    await callback.answer(f"✅ Purchased with wallet. Balance: ${balance:.2f}")


# --------------------------------------------------------------------------- #
# Purchase flow
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None:
        return
    if callback.from_user is None:
        return
    product_id = int(callback.data.split(":", 1)[1])

    async with AsyncSessionLocal() as session:
        if await _deny_if_permanently_blocked(session, callback.from_user.id):
            await callback.answer(
                "🚫 Permanently blocked account.", show_alert=True
            )
            return
        overviews = await services.list_product_overviews(session)
        show_stock = await services.get_bot_show_stock(session)
    selected = next(
        (
            o
            for o in overviews
            if o.product.id == product_id and o.product.is_active
        ),
        None,
    )

    if selected is None:
        await callback.answer(
            "This product is no longer available.", show_alert=True
        )
        return
    if selected.available <= 0:
        await callback.answer("😔 This product is out of stock.", show_alert=True)
        return

    await state.set_state(PurchaseState.waiting_for_quantity)
    await state.update_data(product_id=product_id)

    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Cancel", callback_data="buy_cancel")]
        ]
    )
    await callback.message.answer(
        f"Selected: <b>{selected.product.name}</b>\n"
        + (
            f"Stock left: <b>{selected.available}</b>\n\n"
            if show_stock
            else "\n"
        )
        + "Send the quantity you want to buy (number only).",
        reply_markup=cancel_kb,
    )
    await callback.answer()


@router.callback_query(F.data == "buy_cancel")
async def cb_buy_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Purchase cancelled.")


@router.message(PurchaseState.waiting_for_quantity)
async def msg_buy_quantity(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Please send a valid number, for example: 2")
        return

    quantity = int(text)
    if quantity < 1:
        await message.answer("Quantity must be at least 1.")
        return

    data = await state.get_data()
    product_id = int(data.get("product_id", 0))
    if product_id <= 0:
        await state.clear()
        await message.answer("Session expired. Please use /products again.")
        return

    async with AsyncSessionLocal() as session:
        user = await services.get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        try:
            order = await services.create_order_and_allocate_stock(
                session,
                OrderCreate(
                    user_id=user.id,
                    product_id=product_id,
                    quantity=quantity,
                ),
            )
        except services.UserInactiveError:
            await message.answer(
                "🚫 Your account is suspended. Contact support."
            )
            await state.clear()
            return
        except services.UserPermanentlyBlockedError:
            await message.answer(
                "🚫 Your account has been permanently blocked. Contact support."
            )
            await state.clear()
            return
        except services.OutOfStockError:
            await message.answer(
                "😔 Not enough stock for that quantity. "
                "Try a smaller number or another product."
            )
            return
        except services.ProductNotFoundError:
            await message.answer(
                "This product is no longer available."
            )
            await state.clear()
            return

        payment = await payment_service.create_payment_session(session, order.id)

    await state.clear()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ I've paid — check", callback_data=f"chk:{order.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Cancel order", callback_data=f"cancelpay:{order.id}"
                )
            ],
        ]
    )
    _track_background_task(
        asyncio.create_task(
            _watch_payment_and_auto_deliver(
                message.bot,
                message.chat.id,
                order.id,
                payment.id,
            )
        ),
        f"order-payment:{order.id}:{payment.id}",
    )
    qr_png = payment_service.render_qr_png(payment.qr_string)
    photo = BufferedInputFile(qr_png, filename=f"khqr_order_{order.id}.png")
    await message.answer_photo(
        photo=photo,
        caption=(
            f"🧾 Order <b>#{order.id}</b> — total <b>${order.total_price}</b>\n\n"
            "📲 Scan this KHQR with any Cambodian banking app "
            "(ABA, Bakong, ACLEDA, Wing…) to pay.\n\n"
            "⏱ The QR expires in 15 minutes. "
            "Tap the button below once you've transferred."
        ),
        reply_markup=keyboard,
    )
    


@router.callback_query(F.data.startswith("chk:"))
async def cb_check_payment(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    if callback.from_user is None:
        return
    order_id = int(callback.data.split(":", 1)[1])

    async with AsyncSessionLocal() as session:
        if await _deny_if_permanently_blocked(session, callback.from_user.id):
            await callback.answer(
                "🚫 Permanently blocked account.", show_alert=True
            )
            return
        try:
            order = await services.get_order_with_items(session, order_id)
        except services.OrderNotFoundError:
            await callback.answer("Order not found.", show_alert=True)
            return

        if order.status is OrderStatus.PENDING:
            payment = await payment_service.get_latest_payment(session, order_id)
            if payment is None:
                await callback.answer("No payment session found.", show_alert=True)
                return
            if payment_service.is_payment_expired(payment):
                await services.cancel_order_and_release_inventory(session, order_id)
                await callback.answer(
                    "⌛ Payment expired. Order canceled and stock released.",
                    show_alert=True,
                )
                return
            paid = await _verify_payment_or_alert(callback, payment.md5)
            if paid is None:
                return
            if not paid:
                await callback.answer(
                    "⏳ Payment not detected yet — give it a few seconds "
                    "and try again.",
                    show_alert=True,
                )
                return
            await payment_service.confirm_payment(session, payment.id)
            order = await services.get_order_with_items(session, order_id)

        if order.status is OrderStatus.DELIVERED:
            await callback.answer("This order was already delivered.", show_alert=True)
            return
        if order.status is OrderStatus.CANCELED:
            await callback.answer(
                "This order was canceled and stock has been returned.",
                show_alert=True,
            )
            return

        await services.mark_order_delivered(session, order_id)
        order = await services.get_order_with_items(session, order_id)
        await _deliver_order(callback, order)

    await callback.answer()


@router.callback_query(F.data.startswith("cancelpay:"))
async def cb_cancel_payment(callback: CallbackQuery) -> None:
    if callback.data is None:
        return
    if callback.from_user is None:
        return
    order_id = int(callback.data.split(":", 1)[1])

    async with AsyncSessionLocal() as session:
        if await _deny_if_permanently_blocked(session, callback.from_user.id):
            await callback.answer(
                "🚫 Permanently blocked account.", show_alert=True
            )
            return
        try:
            order = await services.get_order_with_items(session, order_id)
        except services.OrderNotFoundError:
            await callback.answer("Order not found.", show_alert=True)
            return

        if order.status is OrderStatus.DELIVERED:
            await callback.answer("Order is already delivered.", show_alert=True)
            return
        if order.status is OrderStatus.CANCELED:
            await callback.answer("Order is already canceled.", show_alert=True)
            return

        payment = await payment_service.get_latest_payment(session, order_id)
        if payment is None:
            await callback.answer("No payment session found.", show_alert=True)
            return
        paid = await _verify_payment_or_alert(callback, payment.md5)
        if paid is None:
            return
        if paid:
            await callback.answer(
                "Payment already received. Tap 'I've paid — check'.",
                show_alert=True,
            )
            return

        await services.cancel_order_and_release_inventory(session, order_id)

    await callback.answer(
        "Order canceled. Reserved stock was returned.", show_alert=True
    )


async def _watch_payment_and_auto_deliver(
    bot: Bot, chat_id: int, order_id: int, payment_id: int
) -> None:
    """Auto-confirm and deliver once payment is detected in background."""
    paid = await payment_service.poll_payment_until_paid(payment_id)
    if not paid:
        try:
            await bot.send_message(
                chat_id,
                f"⌛ Order #{order_id} expired unpaid. "
                "If needed, please create a new order.",
            )
        except Exception:
            logger.exception("Failed to send expiry notice for order %s", order_id)
        return

    async with AsyncSessionLocal() as session:
        try:
            order = await services.get_order_with_items(session, order_id)
        except services.OrderNotFoundError:
            logger.warning("Auto-delivery skipped: order %s not found", order_id)
            return

        if order.status is OrderStatus.DELIVERED:
            return
        if order.status is OrderStatus.CANCELED:
            logger.info("Auto-delivery skipped: order %s canceled", order_id)
            return

        await services.mark_order_delivered(session, order_id)
        order = await services.get_order_with_items(session, order_id)

    await _deliver_order_to_chat(bot, chat_id, order)


async def _deliver_order(callback: CallbackQuery, order: Order) -> None:
    """Send the purchased inventory data to the buyer."""
    assert callback.message is not None
    await _deliver_order_to_chat(callback.message.bot, callback.message.chat.id, order)


async def _deliver_order_to_chat(
    bot: Bot,
    chat_id: int,
    order: Order,
    title: str = "✅ Payment confirmed",
) -> None:
    """Send purchased inventory data to a chat id.

    Items are grouped per product so every product shows ITS OWN
    warranty and delivery note (an order can mix products).
    """
    grouped: dict[int, list[str]] = {}
    for item in order.items:
        grouped.setdefault(item.product_id, []).append(item.data)

    products: dict[int, Product | None] = {}
    notes: dict[int, str | None] = {}
    async with AsyncSessionLocal() as session:
        for product_id in grouped:
            products[product_id] = await session.get(Product, product_id)
            notes[product_id] = await services.get_product_client_note(
                session, product_id
            )

    sections: list[str] = []
    file_sections: list[str] = []
    for product_id, values in grouped.items():
        product = products.get(product_id)
        name = product.name if product else f"Product {product_id}"
        lines = "\n".join(
            f"• <code>{html.escape(value)}</code>" for value in values
        )
        section = f"📦 <b>{html.escape(name)}</b>\n{lines}"
        file_section = [name] + [f"- {value}" for value in values]
        if product and product.warranty_days:
            section += f"\n🛡 Warranty: {product.warranty_days} days"
            file_section.append(f"Warranty: {product.warranty_days} days")
        note = notes.get(product_id)
        if note:
            section += f"\n📝 Note: {html.escape(note)}"
            file_section.append(f"Note: {note}")
        sections.append(section)
        file_sections.append("\n".join(file_section))

    body = "\n\n".join(sections)
    await bot.send_message(
        chat_id,
        f"{title} — order <b>#{order.id}</b>\n\n"
        f"{body}\n\n"
        "Thank you for your purchase!"
    )

    # Send a downloadable text file so clients can keep order credentials safely.
    text_lines = [
        f"Order #{order.id}",
        "Bondom Account",
        "",
    ]
    text_lines.append("\n\n".join(file_sections))

    order_file = BufferedInputFile(
        "\n".join(text_lines).encode("utf-8"),
        filename=f"order_{order.id}_items.txt",
    )
    await bot.send_document(
        chat_id,
        document=order_file,
        caption=f"Download your order file for order #{order.id}.",
    )
