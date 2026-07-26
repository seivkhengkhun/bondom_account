"""Bondom Account — Reflex admin control panel (pure Python).

Full store control room over the shared database:
  - KPI dashboard (users, orders, revenue, live stock)
  - Add products, edit prices, show/hide products
  - Per-product stock overview: available (before buy) vs sold (after buy)
  - Bulk-upload inventory, clear unsold stock
  - Suspend / reactivate users (bot enforces it on next purchase)

Session handling mirrors the FastAPI backend exactly: every event handler
opens a short-lived ``AsyncSessionLocal`` session and calls the shared
service layer, which owns transaction boundaries. No SQL or business
rules live in the UI.

Run from ``app/web`` with:  reflex run
"""

import dataclasses
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import reflex as rx
from aiogram import Bot

from shared import services
from shared.config import settings
from shared.database import AsyncSessionLocal
from shared.schemas import ProductCreate


# --------------------------------------------------------------------------- #
# Row view-models (state vars must be serializable — Decimals/datetimes
# are converted to strings when loading)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class ProductRow:
    id: int
    name: str
    price: str
    category: str
    warranty_days: int
    is_active: bool
    available: int
    sold: int
    revenue: str


@dataclasses.dataclass
class OrderRow:
    id: int
    buyer: str
    total_price: str
    status: str
    created_at: str


@dataclasses.dataclass
class UserRow:
    id: int
    telegram_id: str
    username: str
    is_active: bool
    is_blocked: bool
    balance: str


@dataclasses.dataclass
class SmsRow:
    id: int
    user: str
    service: str
    country: str
    phone: str
    cost: str
    price: str
    profit: str
    status: str
    otp: str
    created_at: str


class AdminState(rx.State):
    # Auth — every mutating handler re-checks ``authed`` server-side, so
    # the gate holds even against hand-crafted websocket events.
    authed: bool = False
    password_input: str = ""
    login_message: str = ""

    # KPIs
    stat_users: int = 0
    stat_orders: int = 0
    stat_paid: int = 0
    stat_revenue: str = "0.00"
    stat_stock: int = 0

    products: list[ProductRow] = []
    orders: list[OrderRow] = []
    users: list[UserRow] = []
    sms_orders: list[SmsRow] = []

    # SMS activation
    sms_markup: str = "0.03"
    sms_markup_message: str = ""
    sms_stat_completed: int = 0
    sms_stat_waiting: int = 0
    sms_stat_refunded: int = 0
    sms_stat_revenue: str = "0.00"
    sms_stat_cost: str = "0.00"
    sms_stat_profit: str = "0.00"
    sms_search: str = ""

    product_options: list[str] = []
    selected_product: str = ""
    categories: list[str] = []
    upload_message: str = ""
    bot_show_stock: bool = True
    orders_message: str = ""
    announcement_text: str = ""
    announcement_message: str = ""
    users_message: str = ""
    user_adjust_amount: str = ""

    # Table search boxes
    product_search: str = ""
    order_search: str = ""
    user_search: str = ""

    def set_product_search(self, v: str) -> None:
        self.product_search = v

    def set_order_search(self, v: str) -> None:
        self.order_search = v

    def set_user_search(self, v: str) -> None:
        self.user_search = v

    def set_sms_search(self, v: str) -> None:
        self.sms_search = v

    def set_sms_markup(self, v: str) -> None:
        self.sms_markup = v

    @rx.var
    def filtered_sms(self) -> list[SmsRow]:
        q = self.sms_search.strip().lower()
        if not q:
            return self.sms_orders
        return [
            r
            for r in self.sms_orders
            if q in r.user.lower()
            or q in r.country.lower()
            or q in r.phone.lower()
            or q in r.service.lower()
            or q == str(r.id)
        ]

    @rx.var
    def filtered_products(self) -> list[ProductRow]:
        q = self.product_search.strip().lower()
        if not q:
            return self.products
        return [
            p
            for p in self.products
            if q in p.name.lower()
            or q in p.category.lower()
            or q == str(p.id)
        ]

    @rx.var
    def filtered_orders(self) -> list[OrderRow]:
        q = self.order_search.strip().lower()
        if not q:
            return self.orders
        return [
            o
            for o in self.orders
            if q in o.buyer.lower() or q in o.status.lower() or q == str(o.id)
        ]

    @rx.var
    def filtered_users(self) -> list[UserRow]:
        q = self.user_search.strip().lower()
        if not q:
            return self.users
        return [
            u
            for u in self.users
            if q in u.username.lower()
            or q in u.telegram_id
            or q == str(u.id)
        ]

    # Add-product form
    new_name: str = ""
    new_price: str = ""
    new_category: str = ""  # free text — creates a new category
    new_category_choice: str = ""  # picked from existing categories
    new_warranty: str = "0"
    form_message: str = ""

    # Manage-selected-product form
    manage_name: str = ""
    manage_price: str = ""
    manage_warranty: str = "0"
    manage_category_choice: str = ""
    manage_category_new: str = ""
    manage_client_note: str = ""
    manage_stock_lines: str = ""
    manage_message: str = ""

    # ----------------------------------------------------------------- #
    # Auth
    # ----------------------------------------------------------------- #
    def set_password_input(self, v: str) -> None:
        self.password_input = v

    async def login(self) -> None:
        if not settings.admin_password:
            self.login_message = (
                "ADMIN_PASSWORD is not set in .env on the server."
            )
            return
        if self.password_input == settings.admin_password:
            self.authed = True
            self.login_message = ""
            self.password_input = ""
            await self.load_all()
        else:
            self.login_message = "Wrong password."

    def logout(self) -> None:
        self.authed = False

    # ----------------------------------------------------------------- #
    # Loading
    # ----------------------------------------------------------------- #
    async def load_all(self) -> None:
        if not self.authed:
            return
        async with AsyncSessionLocal() as session:
            stats = await services.get_store_stats(session)
            overviews = await services.list_product_overviews(session)
            orders = await services.list_orders(session, limit=100)
            users = await services.list_users(session, limit=200)
            blocked_user_ids = await services.list_blocked_user_ids(session)
            self.bot_show_stock = await services.get_bot_show_stock(session)

        self.stat_users = stats.total_users
        self.stat_orders = stats.total_orders
        self.stat_paid = stats.paid_orders
        self.stat_revenue = f"{stats.revenue:.2f}"
        self.stat_stock = stats.available_stock

        self.products = [
            ProductRow(
                id=o.product.id,
                name=o.product.name,
                price=f"{o.product.price:.2f}",
                category=o.product.category,
                warranty_days=o.product.warranty_days,
                is_active=o.product.is_active,
                available=o.available,
                sold=o.sold,
                revenue=f"{o.revenue:.2f}",
            )
            for o in overviews
        ]
        self.product_options = [
            f"{o.product.id} — {o.product.name}" for o in overviews
        ]
        self.categories = sorted(
            {p.category for p in self.products if p.category}
        )
        if self.selected_product and self.selected_product in self.product_options:
            selected_id = int(self.selected_product.split(" — ", 1)[0])
            selected = next(
                (p for p in self.products if p.id == selected_id), None
            )
            if selected is not None:
                self.manage_name = selected.name
                self.manage_price = selected.price
                self.manage_warranty = str(selected.warranty_days)
                self.manage_category_choice = selected.category
            async with AsyncSessionLocal() as session:
                self.manage_client_note = (
                    await services.get_product_client_note(session, selected_id)
                    or ""
                )
        elif self.product_options:
            self.selected_product = self.product_options[0]
            self.manage_name = self.selected_product.split(" — ", 1)[1]
            selected_id = int(self.selected_product.split(" — ", 1)[0])
            selected = next(
                (p for p in self.products if p.id == selected_id), None
            )
            self.manage_price = selected.price if selected is not None else ""
            self.manage_warranty = (
                str(selected.warranty_days) if selected is not None else "0"
            )
            self.manage_category_choice = (
                selected.category if selected is not None else ""
            )
            async with AsyncSessionLocal() as session:
                self.manage_client_note = (
                    await services.get_product_client_note(session, selected_id)
                    or ""
                )
        else:
            self.selected_product = ""
            self.manage_name = ""
            self.manage_price = ""
            self.manage_warranty = "0"
            self.manage_category_choice = ""
            self.manage_client_note = ""
        self.orders = [
            OrderRow(
                id=o.id,
                buyer=o.user.username or str(o.user.telegram_id),
                total_price=f"{o.total_price:.2f}",
                status=o.status.value,
                created_at=o.created_at.strftime("%Y-%m-%d %H:%M"),
            )
            for o in orders
        ]
        self.users = [
            UserRow(
                id=u.id,
                telegram_id=str(u.telegram_id),
                username=u.username or "—",
                is_active=u.is_active,
                is_blocked=u.id in blocked_user_ids,
                balance="0.00",
            )
            for u in users
        ]
        async with AsyncSessionLocal() as session:
            for row in self.users:
                row.balance = f"{(await services.get_user_balance(session, row.id)):.2f}"

        await self._load_sms()

    async def _load_sms(self) -> None:
        from shared import sms_service

        username_by_id = {u.id: u.username for u in self.users}
        async with AsyncSessionLocal() as session:
            stats = await sms_service.sms_stats(session)
            markup = await sms_service.get_markup(session)
            sms_orders = await sms_service.list_sms_orders(session, limit=300)
            missing = {
                o.user_id for o in sms_orders if o.user_id not in username_by_id
            }
            for uid in missing:
                user = await session.get(services.User, uid)
                username_by_id[uid] = (
                    (user.username or str(user.telegram_id))
                    if user else f"user {uid}"
                )

        self.sms_markup = f"{markup:.2f}"
        self.sms_stat_completed = stats["completed"]
        self.sms_stat_waiting = stats["waiting"]
        self.sms_stat_refunded = stats["refunded"]
        self.sms_stat_revenue = f"{stats['revenue']:.2f}"
        self.sms_stat_cost = f"{stats['cost']:.2f}"
        self.sms_stat_profit = f"{stats['profit']:.2f}"
        self.sms_orders = [
            SmsRow(
                id=o.id,
                user=username_by_id.get(o.user_id, f"user {o.user_id}"),
                service=o.category.title(),
                country=o.country,
                phone=o.phone or "—",
                cost=f"{o.cost:.3f}",
                price=f"{o.price:.2f}",
                profit=f"{(o.price - o.cost):.2f}"
                if o.status.value == "completed" else "0.00",
                status=o.status.value,
                otp=o.otp_code or "—",
                created_at=o.created_at.strftime("%Y-%m-%d %H:%M"),
            )
            for o in sms_orders
        ]

    async def save_sms_markup(self) -> None:
        if not self.authed:
            return
        from decimal import Decimal, InvalidOperation
        from shared import sms_service

        try:
            value = Decimal(self.sms_markup or "0")
        except InvalidOperation:
            self.sms_markup_message = "⚠ Invalid markup amount."
            return
        try:
            async with AsyncSessionLocal() as session:
                stored = await sms_service.set_markup(session, value)
        except sms_service.SmsServiceError as exc:
            self.sms_markup_message = f"⚠ {exc}"
            return
        self.sms_markup = f"{stored:.2f}"
        self.sms_markup_message = (
            f"✅ Markup set to ${stored:.2f} — applies to new purchases."
        )
        await self._load_sms()

    # ----------------------------------------------------------------- #
    # Add product
    # ----------------------------------------------------------------- #
    def set_new_name(self, v: str) -> None:
        self.new_name = v

    def set_new_price(self, v: str) -> None:
        self.new_price = v

    def set_new_category(self, v: str) -> None:
        self.new_category = v

    def set_new_category_choice(self, v: str) -> None:
        self.new_category_choice = v

    def set_manage_category_choice(self, v: str) -> None:
        self.manage_category_choice = v

    def set_manage_category_new(self, v: str) -> None:
        self.manage_category_new = v

    def set_new_warranty(self, v: str) -> None:
        self.new_warranty = v

    async def add_product(self) -> None:
        if not self.authed:
            return
        # A typed new category wins over the dropdown pick.
        category = (
            self.new_category.strip() or self.new_category_choice.strip()
        )
        try:
            payload = ProductCreate(
                name=self.new_name.strip(),
                price=Decimal(self.new_price or "0"),
                category=category or "general",
                warranty_days=int(self.new_warranty or "0"),
            )
        except (InvalidOperation, ValueError) as exc:
            self.form_message = f"⚠ Invalid input: {exc}"
            return

        async with AsyncSessionLocal() as session:
            product = await services.create_product(session, payload)
        self.form_message = (
            f"✅ Added '{product.name}' (#{product.id}) "
            f"in category '{product.category}'."
        )
        self.new_name = self.new_price = self.new_category = ""
        self.new_category_choice = ""
        self.new_warranty = "0"
        await self.load_all()

    # ----------------------------------------------------------------- #
    # Bot settings
    # ----------------------------------------------------------------- #
    async def set_bot_show_stock_toggle(self, enabled: bool) -> None:
        if not self.authed:
            return
        async with AsyncSessionLocal() as session:
            await services.set_bot_show_stock(session, enabled)
        self.bot_show_stock = enabled

    # ----------------------------------------------------------------- #
    # Product controls: price, visibility, clear stock
    # ----------------------------------------------------------------- #
    async def toggle_product(self, product_id: int, is_active: bool) -> None:
        if not self.authed:
            return
        async with AsyncSessionLocal() as session:
            await services.set_product_active(session, product_id, is_active)
        await self.load_all()

    async def clear_stock(self, product_id: int) -> None:
        if not self.authed:
            return
        async with AsyncSessionLocal() as session:
            n = await services.delete_available_inventory(session, product_id)
        self.upload_message = f"🗑 Removed {n} unsold item(s) from product #{product_id}."
        await self.load_all()

    # ----------------------------------------------------------------- #
    # User control: suspend / reactivate (bot checks on next purchase)
    # ----------------------------------------------------------------- #
    async def toggle_user(self, user_id: int, is_active: bool) -> None:
        if not self.authed:
            return
        try:
            async with AsyncSessionLocal() as session:
                await services.toggle_user_status(session, user_id, is_active)
            self.users_message = (
                f"✅ User #{user_id} {'reactivated' if is_active else 'suspended'}."
            )
        except services.UserPermanentlyBlockedError:
            self.users_message = (
                f"🚫 User #{user_id} is permanently blocked and cannot be reactivated."
            )
        await self.load_all()

    async def block_user_forever(self, user_id: int) -> None:
        if not self.authed:
            return
        async with AsyncSessionLocal() as session:
            await services.block_user_forever(session, user_id)
        self.users_message = f"🔒 User #{user_id} permanently blocked."
        await self.load_all()

    async def unblock_user(self, user_id: int) -> None:
        if not self.authed:
            return
        async with AsyncSessionLocal() as session:
            await services.unblock_user(session, user_id)
        self.users_message = f"✅ User #{user_id} unblocked and reactivated."
        await self.load_all()

    def set_user_adjust_amount(self, value: str) -> None:
        self.user_adjust_amount = value

    async def credit_user_wallet(self, user_id: int) -> None:
        if not self.authed:
            return
        try:
            amount = Decimal(self.user_adjust_amount or "0")
        except InvalidOperation:
            self.users_message = "⚠ Invalid wallet amount."
            return
        if amount <= 0:
            self.users_message = "⚠ Enter amount greater than 0."
            return

        async with AsyncSessionLocal() as session:
            updated = await services.adjust_user_balance(session, user_id, amount)
        self.users_message = (
            f"✅ Credited ${amount:.2f} to user #{user_id}. "
            f"Balance: ${updated:.2f}."
        )
        await self.load_all()

    async def debit_user_wallet(self, user_id: int) -> None:
        if not self.authed:
            return
        try:
            amount = Decimal(self.user_adjust_amount or "0")
        except InvalidOperation:
            self.users_message = "⚠ Invalid wallet amount."
            return
        if amount <= 0:
            self.users_message = "⚠ Enter amount greater than 0."
            return

        try:
            async with AsyncSessionLocal() as session:
                updated = await services.adjust_user_balance(
                    session, user_id, Decimal("0") - amount
                )
            self.users_message = (
                f"✅ Debited ${amount:.2f} from user #{user_id}. "
                f"Balance: ${updated:.2f}."
            )
        except services.InsufficientBalanceError as exc:
            self.users_message = (
                f"⚠ Cannot debit user #{user_id}. "
                f"Need ${exc.required:.2f}, have ${exc.balance:.2f}."
            )
        await self.load_all()

    # ----------------------------------------------------------------- #
    # Bulk inventory upload (one item per line)
    # ----------------------------------------------------------------- #
    async def set_product(self, value: str) -> None:
        """Switch selected product and reload EVERY per-product field.

        The delivery note is loaded fresh from the DB here — otherwise the
        previous product's note lingers in the textarea and gets saved onto
        the newly selected product (1 product = 1 note must hold).
        """
        self.selected_product = value
        selected_id = int(value.split(" — ", 1)[0])
        selected = next((p for p in self.products if p.id == selected_id), None)
        self.manage_name = selected.name if selected is not None else ""
        self.manage_price = selected.price if selected is not None else ""
        self.manage_warranty = (
            str(selected.warranty_days) if selected is not None else "0"
        )
        self.manage_category_choice = (
            selected.category if selected is not None else ""
        )
        self.manage_category_new = ""
        async with AsyncSessionLocal() as session:
            self.manage_client_note = (
                await services.get_product_client_note(session, selected_id)
                or ""
            )

    def set_manage_price(self, value: str) -> None:
        self.manage_price = value

    def set_manage_warranty(self, value: str) -> None:
        self.manage_warranty = value

    def set_manage_client_note(self, value: str) -> None:
        self.manage_client_note = value

    def set_manage_name(self, value: str) -> None:
        self.manage_name = value

    def set_manage_stock_lines(self, value: str) -> None:
        self.manage_stock_lines = value

    def set_announcement_text(self, value: str) -> None:
        self.announcement_text = value

    def _selected_product_id(self) -> int:
        if self.selected_product:
            return int(self.selected_product.split(" — ", 1)[0])
        if self.product_options:
            self.selected_product = self.product_options[0]
            return int(self.selected_product.split(" — ", 1)[0])
        return 0

    async def handle_upload(self, files: list[rx.UploadFile]) -> None:
        if not self.authed:
            return
        if not files:
            self.upload_message = "⚠ No file selected."
            return

        product_id = self._selected_product_id()
        if product_id <= 0:
            self.upload_message = "⚠ Select a product first."
            return

        inserted = 0
        skipped_empty = 0
        skipped_duplicate = 0
        async with AsyncSessionLocal() as session:
            for file in files:
                content = (await file.read()).decode("utf-8", errors="replace")
                report = await services.bulk_add_inventory_with_report(
                    session, product_id, content.splitlines()
                )
                inserted += report.inserted
                skipped_empty += report.skipped_empty
                skipped_duplicate += report.skipped_duplicate
        self.upload_message = (
            f"✅ Uploaded {inserted} account(s). "
            f"Skipped empty: {skipped_empty}, duplicates: {skipped_duplicate}."
        )
        await self.load_all()

    async def add_stock_to_selected(self) -> None:
        if not self.authed:
            return
        product_id = self._selected_product_id()
        if product_id <= 0:
            self.manage_message = "⚠ Select a product first."
            return

        lines = self.manage_stock_lines.splitlines()
        async with AsyncSessionLocal() as session:
            report = await services.bulk_add_inventory_with_report(
                session, product_id, lines
            )
        self.manage_message = (
            f"✅ Added {report.inserted} account(s) to product #{product_id}. "
            f"Skipped empty: {report.skipped_empty}, "
            f"duplicates: {report.skipped_duplicate}."
        )
        self.manage_stock_lines = ""
        await self.load_all()

    async def rename_selected_product(self) -> None:
        if not self.authed:
            return
        product_id = self._selected_product_id()
        if product_id <= 0:
            self.manage_message = "⚠ Select a product first."
            return
        new_name = self.manage_name.strip()
        if not new_name:
            self.manage_message = "⚠ Product name cannot be empty."
            return

        async with AsyncSessionLocal() as session:
            await services.rename_product(session, product_id, new_name)
        self.manage_message = f"✅ Renamed product #{product_id}."
        await self.load_all()

    async def update_selected_price(self) -> None:
        if not self.authed:
            return
        product_id = self._selected_product_id()
        if product_id <= 0:
            self.manage_message = "⚠ Select a product first."
            return

        try:
            new_price = Decimal(self.manage_price or "0")
        except InvalidOperation:
            self.manage_message = "⚠ Invalid price."
            return
        if new_price <= 0:
            self.manage_message = "⚠ Price must be greater than 0."
            return

        async with AsyncSessionLocal() as session:
            await services.update_product_price(session, product_id, new_price)
        self.manage_message = f"✅ Updated price for product #{product_id}."
        await self.load_all()

    async def update_selected_warranty(self) -> None:
        if not self.authed:
            return
        product_id = self._selected_product_id()
        if product_id <= 0:
            self.manage_message = "⚠ Select a product first."
            return

        try:
            warranty_days = int(self.manage_warranty or "0")
        except ValueError:
            self.manage_message = "⚠ Invalid warranty days."
            return
        if warranty_days < 0:
            self.manage_message = "⚠ Warranty days must be 0 or more."
            return

        async with AsyncSessionLocal() as session:
            await services.update_product_warranty_days(
                session, product_id, warranty_days
            )
        self.manage_message = (
            f"✅ Updated warranty for product #{product_id} to {warranty_days} day(s)."
        )
        await self.load_all()

    async def update_selected_category(self) -> None:
        if not self.authed:
            return
        product_id = self._selected_product_id()
        if product_id <= 0:
            self.manage_message = "⚠ Select a product first."
            return

        # A typed new category wins over the dropdown pick.
        category = (
            self.manage_category_new.strip()
            or self.manage_category_choice.strip()
        )
        if not category:
            self.manage_message = (
                "⚠ Pick an existing category or type a new one."
            )
            return

        async with AsyncSessionLocal() as session:
            await services.update_product_category(
                session, product_id, category
            )
        self.manage_message = (
            f"✅ Moved product #{product_id} to category '{category}'."
        )
        self.manage_category_new = ""
        self.manage_category_choice = category
        await self.load_all()

    async def save_client_note(self) -> None:
        if not self.authed:
            return
        product_id = self._selected_product_id()
        if product_id <= 0:
            self.manage_message = "⚠ Select a product first."
            return

        async with AsyncSessionLocal() as session:
            saved = await services.set_product_client_note(
                session, product_id, self.manage_client_note
            )
        self.manage_message = (
            f"✅ Saved note for product #{product_id}."
            if saved
            else f"✅ Cleared note for product #{product_id}."
        )
        await self.load_all()

    async def delete_selected_product(self) -> None:
        if not self.authed:
            return
        product_id = self._selected_product_id()
        if product_id <= 0:
            self.manage_message = "⚠ Select a product first."
            return

        async with AsyncSessionLocal() as session:
            await services.delete_product(session, product_id)

        self.manage_message = f"🗑 Deleted product #{product_id}."
        self.manage_name = ""
        await self.load_all()

    async def clear_orders_now(self) -> None:
        if not self.authed:
            return
        async with AsyncSessionLocal() as session:
            deleted = await services.clear_all_orders_for_fresh_revenue(session)
        self.orders_message = (
            f"🧹 Cleared {deleted} order(s). Revenue now starts from this point."
        )
        await self.load_all()

    async def publish_announcement(self) -> None:
        if not self.authed:
            return
        message = self.announcement_text.strip()
        if not message:
            self.announcement_message = "⚠ Write announcement message first."
            return
        if not settings.bot_token:
            self.announcement_message = "⚠ BOT_TOKEN is not set."
            return

        async with AsyncSessionLocal() as session:
            telegram_ids = await services.list_active_telegram_ids(session)

        if not telegram_ids:
            self.announcement_message = "ℹ No active users to receive announcement."
            return

        sent = 0
        failed = 0
        bot = Bot(token=settings.bot_token)
        try:
            for chat_id in telegram_ids:
                try:
                    await bot.send_message(chat_id=chat_id, text=message)
                    sent += 1
                except Exception:
                    failed += 1
        finally:
            await bot.session.close()

        self.announcement_message = (
            f"📣 Announcement published. Sent: {sent}, failed: {failed}."
        )
        self.announcement_text = ""


# --------------------------------------------------------------------------- #
# UI components
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# UI building blocks
# --------------------------------------------------------------------------- #
def section_message(msg) -> rx.Component:
    """Inline feedback callout — hidden while the message is empty."""
    return rx.cond(
        msg != "",
        rx.callout(msg, icon="info", size="1", variant="surface", width="100%"),
    )


def card_header(icon_name: str, title: str, subtitle: str = "") -> rx.Component:
    rows = [
        rx.hstack(
            rx.icon(icon_name, size=18, color=rx.color("accent", 9)),
            rx.heading(title, size="4"),
            spacing="2",
            align="center",
        )
    ]
    if subtitle:
        rows.append(rx.text(subtitle, size="1", color_scheme="gray"))
    return rx.vstack(*rows, spacing="1", width="100%")


def search_box(placeholder: str, value, on_change) -> rx.Component:
    return rx.input(
        rx.input.slot(rx.icon("search", size=14)),
        placeholder=placeholder,
        value=value,
        on_change=on_change,
        width="16em",
        size="2",
        variant="surface",
    )


def stat_card(icon_name: str, label: str, value, accent: str) -> rx.Component:
    return rx.card(
        rx.hstack(
            rx.box(
                rx.icon(icon_name, size=20, color=rx.color(accent, 9)),
                background_color=rx.color(accent, 3),
                border_radius="10px",
                padding="0.55em",
            ),
            rx.vstack(
                rx.text(label, size="1", color_scheme="gray", weight="medium"),
                rx.heading(value, size="6"),
                spacing="0",
            ),
            spacing="3",
            align="center",
        ),
        size="2",
    )


def kpi_row() -> rx.Component:
    return rx.grid(
        stat_card("users", "Users", AdminState.stat_users, "blue"),
        stat_card("shopping-cart", "Orders", AdminState.stat_orders, "violet"),
        stat_card("badge-check", "Paid orders", AdminState.stat_paid, "green"),
        stat_card("dollar-sign", "Revenue", AdminState.stat_revenue, "green"),
        stat_card("boxes", "Stock left", AdminState.stat_stock, "amber"),
        columns=rx.breakpoints(initial="2", sm="3", lg="5"),
        spacing="3",
        width="100%",
    )


# --------------------------------------------------------------------------- #
# Products tab
# --------------------------------------------------------------------------- #
def category_picker(
    choice_value, on_choice, new_value, on_new, new_placeholder: str
) -> rx.Component:
    """Pick an existing category OR type a new one (typed name wins)."""
    return rx.hstack(
        rx.select(
            AdminState.categories,
            value=choice_value,
            on_change=on_choice,
            placeholder="Existing category…",
            width="14em",
        ),
        rx.text("or", size="1", color_scheme="gray"),
        rx.input(
            placeholder=new_placeholder,
            value=new_value,
            on_change=on_new,
            width="14em",
        ),
        spacing="2",
        align="center",
        wrap="wrap",
    )


def add_product_card() -> rx.Component:
    return rx.card(
        rx.vstack(
            card_header(
                "package-plus",
                "Add Product",
                "Pick an existing category or type a new one to create it.",
            ),
            rx.grid(
                rx.vstack(
                    rx.text("Name", size="1", weight="medium"),
                    rx.input(
                        placeholder="e.g. Netflix Premium 1 Month",
                        value=AdminState.new_name,
                        on_change=AdminState.set_new_name,
                        width="100%",
                    ),
                    spacing="1",
                ),
                rx.vstack(
                    rx.text("Price (USD)", size="1", weight="medium"),
                    rx.input(
                        placeholder="0.00",
                        type="number",
                        value=AdminState.new_price,
                        on_change=AdminState.set_new_price,
                        width="100%",
                    ),
                    spacing="1",
                ),
                rx.vstack(
                    rx.text("Warranty (days)", size="1", weight="medium"),
                    rx.input(
                        placeholder="0",
                        type="number",
                        value=AdminState.new_warranty,
                        on_change=AdminState.set_new_warranty,
                        width="100%",
                    ),
                    spacing="1",
                ),
                columns=rx.breakpoints(initial="1", sm="3"),
                spacing="3",
                width="100%",
            ),
            rx.vstack(
                rx.text("Category", size="1", weight="medium"),
                category_picker(
                    AdminState.new_category_choice,
                    AdminState.set_new_category_choice,
                    AdminState.new_category,
                    AdminState.set_new_category,
                    "new category name",
                ),
                spacing="1",
                width="100%",
            ),
            rx.button(
                rx.icon("plus", size=16),
                "Add Product",
                on_click=AdminState.add_product,
                size="2",
            ),
            section_message(AdminState.form_message),
            spacing="4",
            width="100%",
        ),
        size="3",
        width="100%",
    )


def _product_row(p: ProductRow) -> rx.Component:
    return rx.table.row(
        rx.table.cell(rx.text(p.id, color_scheme="gray")),
        rx.table.cell(rx.text(p.name, weight="medium")),
        rx.table.cell("$" + p.price),
        rx.table.cell(rx.badge(p.category, variant="surface")),
        rx.table.cell(
            rx.badge(
                p.available,
                color_scheme=rx.cond(p.available > 0, "green", "red"),
                variant="soft",
            )
        ),
        rx.table.cell(rx.badge(p.sold, color_scheme="blue", variant="soft")),
        rx.table.cell("$" + p.revenue),
        rx.table.cell(
            rx.switch(
                checked=p.is_active,
                on_change=lambda checked: AdminState.toggle_product(
                    p.id, checked
                ),
                size="1",
            )
        ),
        rx.table.cell(
            rx.button(
                rx.icon("eraser", size=14),
                "Clear stock",
                size="1",
                color_scheme="red",
                variant="soft",
                on_click=lambda: AdminState.clear_stock(p.id),
            )
        ),
        align="center",
    )


def products_table_card() -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.hstack(
                card_header(
                    "boxes",
                    "Products & Stock",
                    "Available = ready to sell · Sold = delivered to orders.",
                ),
                rx.spacer(),
                search_box(
                    "Search products…",
                    AdminState.product_search,
                    AdminState.set_product_search,
                ),
                width="100%",
                align="start",
                wrap="wrap",
            ),
            rx.box(
                rx.table.root(
                    rx.table.header(
                        rx.table.row(
                            rx.table.column_header_cell("ID"),
                            rx.table.column_header_cell("Name"),
                            rx.table.column_header_cell("Price"),
                            rx.table.column_header_cell("Category"),
                            rx.table.column_header_cell("Available"),
                            rx.table.column_header_cell("Sold"),
                            rx.table.column_header_cell("Revenue"),
                            rx.table.column_header_cell("Active"),
                            rx.table.column_header_cell(""),
                        )
                    ),
                    rx.table.body(
                        rx.foreach(AdminState.filtered_products, _product_row)
                    ),
                    variant="surface",
                    size="2",
                    width="100%",
                ),
                overflow_x="auto",
                width="100%",
            ),
            spacing="4",
            width="100%",
        ),
        size="3",
        width="100%",
    )


def field_row(label: str, control, button) -> rx.Component:
    return rx.vstack(
        rx.text(label, size="1", weight="medium"),
        rx.hstack(control, button, spacing="2", align="center", wrap="wrap"),
        spacing="1",
        width="100%",
    )


def manage_product_card() -> rx.Component:
    return rx.card(
        rx.vstack(
            card_header(
                "settings-2",
                "Manage Selected Product",
                "Every field below belongs ONLY to the selected product.",
            ),
            rx.select(
                AdminState.product_options,
                value=AdminState.selected_product,
                on_change=AdminState.set_product,
                placeholder="Select product…",
                width="22em",
            ),
            rx.grid(
                field_row(
                    "Rename",
                    rx.input(
                        placeholder="New product name",
                        value=AdminState.manage_name,
                        on_change=AdminState.set_manage_name,
                        width="16em",
                    ),
                    rx.button(
                        "Rename",
                        on_click=AdminState.rename_selected_product,
                        variant="soft",
                        size="2",
                    ),
                ),
                field_row(
                    "Price (USD)",
                    rx.input(
                        placeholder="0.00",
                        type="number",
                        value=AdminState.manage_price,
                        on_change=AdminState.set_manage_price,
                        width="10em",
                    ),
                    rx.button(
                        "Update Price",
                        on_click=AdminState.update_selected_price,
                        variant="soft",
                        size="2",
                    ),
                ),
                field_row(
                    "Warranty (days, 0 = none)",
                    rx.input(
                        placeholder="0",
                        type="number",
                        value=AdminState.manage_warranty,
                        on_change=AdminState.set_manage_warranty,
                        width="10em",
                    ),
                    rx.button(
                        "Update Warranty",
                        on_click=AdminState.update_selected_warranty,
                        variant="soft",
                        size="2",
                    ),
                ),
                field_row(
                    "Category (pick existing or type new)",
                    category_picker(
                        AdminState.manage_category_choice,
                        AdminState.set_manage_category_choice,
                        AdminState.manage_category_new,
                        AdminState.set_manage_category_new,
                        "new category name",
                    ),
                    rx.button(
                        "Update Category",
                        on_click=AdminState.update_selected_category,
                        variant="soft",
                        size="2",
                    ),
                ),
                columns=rx.breakpoints(initial="1", md="2"),
                spacing="4",
                width="100%",
            ),
            rx.divider(),
            rx.vstack(
                rx.hstack(
                    rx.icon("sticky-note", size=16, color=rx.color("amber", 9)),
                    rx.text(
                        "Delivery note — sent to the buyer with THIS product "
                        "only. Each product keeps its own note.",
                        size="1",
                        weight="medium",
                    ),
                    spacing="2",
                    align="center",
                ),
                rx.text_area(
                    placeholder="Optional note (leave blank for no note)",
                    value=AdminState.manage_client_note,
                    on_change=AdminState.set_manage_client_note,
                    width="100%",
                    min_height="6em",
                ),
                rx.button(
                    rx.icon("save", size=16),
                    "Save Note for This Product",
                    on_click=AdminState.save_client_note,
                    size="2",
                ),
                spacing="2",
                width="100%",
            ),
            rx.divider(),
            rx.vstack(
                rx.hstack(
                    rx.icon("list-plus", size=16, color=rx.color("green", 9)),
                    rx.text(
                        "Add stock — one credential/key per line.",
                        size="1",
                        weight="medium",
                    ),
                    spacing="2",
                    align="center",
                ),
                rx.text_area(
                    placeholder="account1|password1\naccount2|password2",
                    value=AdminState.manage_stock_lines,
                    on_change=AdminState.set_manage_stock_lines,
                    width="100%",
                    min_height="8em",
                ),
                rx.hstack(
                    rx.button(
                        rx.icon("plus", size=16),
                        "Add Stock",
                        color_scheme="green",
                        on_click=AdminState.add_stock_to_selected,
                        size="2",
                    ),
                    rx.spacer(),
                    rx.button(
                        rx.icon("trash-2", size=16),
                        "Delete Product",
                        color_scheme="red",
                        variant="soft",
                        on_click=AdminState.delete_selected_product,
                        size="2",
                    ),
                    width="100%",
                ),
                spacing="2",
                width="100%",
            ),
            section_message(AdminState.manage_message),
            spacing="4",
            width="100%",
        ),
        size="3",
        width="100%",
    )


def _bulk_upload_body() -> rx.Component:
    return rx.vstack(
        rx.select(
            AdminState.product_options,
            value=AdminState.selected_product,
            on_change=AdminState.set_product,
            placeholder="Select product…",
            width="22em",
        ),
        rx.upload(
            rx.vstack(
                rx.icon("file-up", size=22, color=rx.color("accent", 9)),
                rx.text("Drop a .txt file here or click to browse", size="2"),
                spacing="2",
                align="center",
            ),
            id="bulk_inventory",
            max_files=5,
            border=f"1.5px dashed {rx.color('accent', 8)}",
            border_radius="12px",
            padding="2em",
            width="100%",
        ),
        rx.hstack(rx.foreach(rx.selected_files("bulk_inventory"), rx.text)),
        rx.button(
            rx.icon("upload", size=16),
            "Bulk Upload",
            on_click=AdminState.handle_upload(
                rx.upload_files(upload_id="bulk_inventory")
            ),
            size="2",
        ),
        section_message(AdminState.upload_message),
        width="100%",
        spacing="3",
    )


def bulk_upload_card_v2() -> rx.Component:
    return rx.card(
        rx.vstack(
            card_header(
                "upload",
                "Bulk Upload Inventory",
                "Pick a product, then upload a .txt file with one item "
                "(credential / key) per line.",
            ),
            _bulk_upload_body(),
            spacing="4",
            width="100%",
        ),
        size="3",
        width="100%",
    )


def products_tab() -> rx.Component:
    return rx.vstack(
        add_product_card(),
        products_table_card(),
        manage_product_card(),
        bulk_upload_card_v2(),
        spacing="4",
        width="100%",
    )


# --------------------------------------------------------------------------- #
# Orders tab
# --------------------------------------------------------------------------- #
def _order_row(o: OrderRow) -> rx.Component:
    return rx.table.row(
        rx.table.cell(rx.text(o.id, color_scheme="gray")),
        rx.table.cell(rx.text(o.buyer, weight="medium")),
        rx.table.cell("$" + o.total_price),
        rx.table.cell(
            rx.badge(
                o.status,
                color_scheme=rx.match(
                    o.status,
                    ("paid", "green"),
                    ("delivered", "green"),
                    ("pending", "amber"),
                    ("canceled", "red"),
                    "gray",
                ),
                variant="soft",
            )
        ),
        rx.table.cell(rx.text(o.created_at, color_scheme="gray", size="1")),
        align="center",
    )


def orders_tab() -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.hstack(
                card_header("shopping-cart", "Orders", "Latest 100 orders."),
                rx.spacer(),
                search_box(
                    "Search orders…",
                    AdminState.order_search,
                    AdminState.set_order_search,
                ),
                rx.button(
                    rx.icon("trash-2", size=14),
                    "Clear Orders",
                    color_scheme="red",
                    variant="soft",
                    size="2",
                    on_click=AdminState.clear_orders_now,
                ),
                width="100%",
                align="start",
                wrap="wrap",
                spacing="3",
            ),
            section_message(AdminState.orders_message),
            rx.box(
                rx.table.root(
                    rx.table.header(
                        rx.table.row(
                            rx.table.column_header_cell("ID"),
                            rx.table.column_header_cell("Buyer"),
                            rx.table.column_header_cell("Total"),
                            rx.table.column_header_cell("Status"),
                            rx.table.column_header_cell("Created"),
                        )
                    ),
                    rx.table.body(
                        rx.foreach(AdminState.filtered_orders, _order_row)
                    ),
                    variant="surface",
                    size="2",
                    width="100%",
                ),
                overflow_x="auto",
                width="100%",
            ),
            spacing="4",
            width="100%",
        ),
        size="3",
        width="100%",
    )


# --------------------------------------------------------------------------- #
# Users tab
# --------------------------------------------------------------------------- #
def _user_row(u: UserRow) -> rx.Component:
    return rx.table.row(
        rx.table.cell(rx.text(u.id, color_scheme="gray")),
        rx.table.cell(u.telegram_id),
        rx.table.cell(rx.text(u.username, weight="medium")),
        rx.table.cell("$" + u.balance),
        rx.table.cell(
            rx.badge(
                rx.cond(u.is_blocked, "Blocked", "Allowed"),
                color_scheme=rx.cond(u.is_blocked, "red", "green"),
                variant="soft",
            )
        ),
        rx.table.cell(
            rx.switch(
                checked=u.is_active,
                on_change=lambda checked: AdminState.toggle_user(u.id, checked),
                size="1",
            )
        ),
        rx.table.cell(
            rx.cond(
                u.is_blocked,
                rx.button(
                    "Unblock",
                    size="1",
                    color_scheme="green",
                    on_click=lambda: AdminState.unblock_user(u.id),
                ),
                rx.button(
                    "Block Forever",
                    size="1",
                    color_scheme="red",
                    variant="soft",
                    on_click=lambda: AdminState.block_user_forever(u.id),
                ),
            )
        ),
        rx.table.cell(
            rx.hstack(
                rx.button(
                    rx.icon("plus", size=14),
                    size="1",
                    color_scheme="green",
                    variant="soft",
                    on_click=lambda: AdminState.credit_user_wallet(u.id),
                ),
                rx.button(
                    rx.icon("minus", size=14),
                    size="1",
                    color_scheme="orange",
                    variant="soft",
                    on_click=lambda: AdminState.debit_user_wallet(u.id),
                ),
                spacing="1",
            )
        ),
        align="center",
    )


def users_tab() -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.hstack(
                card_header(
                    "users",
                    "Users",
                    "Toggle Active to suspend; +/- adjusts wallet by the "
                    "amount below.",
                ),
                rx.spacer(),
                search_box(
                    "Search users…",
                    AdminState.user_search,
                    AdminState.set_user_search,
                ),
                width="100%",
                align="start",
                wrap="wrap",
            ),
            rx.hstack(
                rx.text("Wallet adjust amount (USD):", size="2"),
                rx.input(
                    placeholder="e.g. 5.00",
                    type="number",
                    value=AdminState.user_adjust_amount,
                    on_change=AdminState.set_user_adjust_amount,
                    width="9em",
                ),
                spacing="2",
                align="center",
            ),
            section_message(AdminState.users_message),
            rx.box(
                rx.table.root(
                    rx.table.header(
                        rx.table.row(
                            rx.table.column_header_cell("ID"),
                            rx.table.column_header_cell("Telegram ID"),
                            rx.table.column_header_cell("Username"),
                            rx.table.column_header_cell("Wallet"),
                            rx.table.column_header_cell("Status"),
                            rx.table.column_header_cell("Active"),
                            rx.table.column_header_cell("Block"),
                            rx.table.column_header_cell("Wallet +/-"),
                        )
                    ),
                    rx.table.body(
                        rx.foreach(AdminState.filtered_users, _user_row)
                    ),
                    variant="surface",
                    size="2",
                    width="100%",
                ),
                overflow_x="auto",
                width="100%",
            ),
            spacing="4",
            width="100%",
        ),
        size="3",
        width="100%",
    )


# --------------------------------------------------------------------------- #
# Marketing / settings tab
# --------------------------------------------------------------------------- #
def marketing_tab() -> rx.Component:
    return rx.vstack(
        rx.card(
            rx.vstack(
                card_header(
                    "megaphone",
                    "Announcement",
                    "Broadcast a message to all active Telegram clients.",
                ),
                rx.text_area(
                    placeholder="Write announcement for clients…",
                    value=AdminState.announcement_text,
                    on_change=AdminState.set_announcement_text,
                    width="100%",
                    min_height="8em",
                ),
                rx.button(
                    rx.icon("send", size=16),
                    "Publish Announcement",
                    on_click=AdminState.publish_announcement,
                    size="2",
                ),
                section_message(AdminState.announcement_message),
                spacing="3",
                width="100%",
            ),
            size="3",
            width="100%",
        ),
        rx.card(
            rx.vstack(
                card_header("bot", "Bot Settings"),
                rx.hstack(
                    rx.switch(
                        checked=AdminState.bot_show_stock,
                        on_change=AdminState.set_bot_show_stock_toggle,
                    ),
                    rx.text(
                        "Show exact stock count in the bot's product list",
                        size="2",
                    ),
                    spacing="3",
                    align="center",
                ),
                spacing="3",
                width="100%",
            ),
            size="3",
            width="100%",
        ),
        spacing="4",
        width="100%",
    )


# --------------------------------------------------------------------------- #
# SMS activation tab
# --------------------------------------------------------------------------- #
def _sms_status_badge(status) -> rx.Component:
    return rx.badge(
        rx.match(
            status,
            ("completed", "Code received"),
            ("waiting_sms", "Waiting SMS"),
            ("refunded", "Refunded"),
            ("failed", "Failed"),
            status,
        ),
        color_scheme=rx.match(
            status,
            ("completed", "green"),
            ("waiting_sms", "amber"),
            ("refunded", "gray"),
            ("failed", "red"),
            "gray",
        ),
        variant="soft",
    )


def _sms_row(r: SmsRow) -> rx.Component:
    return rx.table.row(
        rx.table.cell(rx.text(r.id, color_scheme="gray")),
        rx.table.cell(rx.text(r.user, weight="medium")),
        rx.table.cell(r.service),
        rx.table.cell(r.country),
        rx.table.cell(rx.text(r.phone, size="1")),
        rx.table.cell("$" + r.cost),
        rx.table.cell("$" + r.price),
        rx.table.cell(rx.text("$" + r.profit, color_scheme="green")),
        rx.table.cell(_sms_status_badge(r.status)),
        rx.table.cell(
            rx.cond(
                r.otp != "—",
                rx.code(r.otp),
                rx.text("—", color_scheme="gray"),
            )
        ),
        rx.table.cell(rx.text(r.created_at, color_scheme="gray", size="1")),
        align="center",
    )


def sms_tab() -> rx.Component:
    return rx.vstack(
        rx.grid(
            stat_card("banknote", "SMS revenue", "$" + AdminState.sms_stat_revenue, "green"),
            stat_card("credit-card", "Provider cost", "$" + AdminState.sms_stat_cost, "amber"),
            stat_card("trending-up", "Profit", "$" + AdminState.sms_stat_profit, "green"),
            stat_card("badge-check", "Completed", AdminState.sms_stat_completed, "blue"),
            stat_card("hourglass", "Waiting", AdminState.sms_stat_waiting, "amber"),
            columns=rx.breakpoints(initial="2", sm="3", lg="5"),
            spacing="3",
            width="100%",
        ),
        rx.card(
            rx.vstack(
                card_header(
                    "percent",
                    "Profit margin (markup)",
                    "Added to every SMS number's provider cost. Applies to "
                    "new purchases immediately.",
                ),
                rx.hstack(
                    rx.text("$", size="4", weight="bold"),
                    rx.input(
                        type="number",
                        value=AdminState.sms_markup,
                        on_change=AdminState.set_sms_markup,
                        width="8em",
                        size="3",
                    ),
                    rx.button(
                        rx.icon("save", size=16),
                        "Save markup",
                        on_click=AdminState.save_sms_markup,
                        size="2",
                    ),
                    spacing="2",
                    align="center",
                ),
                section_message(AdminState.sms_markup_message),
                spacing="3",
                width="100%",
            ),
            size="3",
            width="100%",
        ),
        rx.card(
            rx.vstack(
                rx.hstack(
                    card_header(
                        "message-square-text",
                        "SMS order history",
                        "Every rented number with cost, price, and profit.",
                    ),
                    rx.spacer(),
                    search_box(
                        "Search SMS…",
                        AdminState.sms_search,
                        AdminState.set_sms_search,
                    ),
                    width="100%",
                    align="start",
                    wrap="wrap",
                ),
                rx.box(
                    rx.table.root(
                        rx.table.header(
                            rx.table.row(
                                rx.table.column_header_cell("ID"),
                                rx.table.column_header_cell("User"),
                                rx.table.column_header_cell("Service"),
                                rx.table.column_header_cell("Country"),
                                rx.table.column_header_cell("Phone"),
                                rx.table.column_header_cell("Cost"),
                                rx.table.column_header_cell("Price"),
                                rx.table.column_header_cell("Profit"),
                                rx.table.column_header_cell("Status"),
                                rx.table.column_header_cell("Code"),
                                rx.table.column_header_cell("Time"),
                            )
                        ),
                        rx.table.body(
                            rx.foreach(AdminState.filtered_sms, _sms_row)
                        ),
                        variant="surface",
                        size="1",
                        width="100%",
                    ),
                    overflow_x="auto",
                    width="100%",
                ),
                spacing="4",
                width="100%",
            ),
            size="3",
            width="100%",
        ),
        spacing="4",
        width="100%",
    )


# --------------------------------------------------------------------------- #
# Page shell
# --------------------------------------------------------------------------- #
def _tab_trigger(icon_name: str, label: str, value: str) -> rx.Component:
    return rx.tabs.trigger(
        rx.hstack(
            rx.icon(icon_name, size=15),
            rx.text(label),
            spacing="2",
            align="center",
        ),
        value=value,
    )


def topbar() -> rx.Component:
    return rx.box(
        rx.hstack(
            rx.hstack(
                rx.icon("store", size=22, color=rx.color("accent", 9)),
                rx.heading("Bondom Admin", size="5"),
                spacing="2",
                align="center",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("refresh-cw", size=15),
                rx.text("Refresh", display=rx.breakpoints(initial="none", sm="block")),
                on_click=AdminState.load_all,
                variant="soft",
                size="2",
            ),
            rx.button(
                rx.icon("log-out", size=15),
                rx.text("Sign out", display=rx.breakpoints(initial="none", sm="block")),
                on_click=AdminState.logout,
                variant="soft",
                color_scheme="gray",
                size="2",
            ),
            width="100%",
            align="center",
            spacing="3",
        ),
        position="sticky",
        top="0",
        z_index="10",
        backdrop_filter="blur(10px)",
        background_color=rx.color("gray", 2),
        border_bottom=f"1px solid {rx.color('gray', 5)}",
        padding="0.7em 1.2em",
        width="100%",
    )


def dashboard_view() -> rx.Component:
    return rx.vstack(
        topbar(),
        rx.box(
            rx.vstack(
                kpi_row(),
                rx.tabs.root(
                    rx.tabs.list(
                        _tab_trigger("boxes", "Products", "products"),
                        _tab_trigger("shopping-cart", "Orders", "orders"),
                        _tab_trigger("users", "Users", "users"),
                        _tab_trigger("smartphone", "SMS", "sms"),
                        _tab_trigger("megaphone", "Marketing", "marketing"),
                        size="2",
                    ),
                    rx.tabs.content(
                        products_tab(), value="products", padding_top="1.2em"
                    ),
                    rx.tabs.content(
                        orders_tab(), value="orders", padding_top="1.2em"
                    ),
                    rx.tabs.content(
                        users_tab(), value="users", padding_top="1.2em"
                    ),
                    rx.tabs.content(
                        sms_tab(), value="sms", padding_top="1.2em"
                    ),
                    rx.tabs.content(
                        marketing_tab(), value="marketing", padding_top="1.2em"
                    ),
                    default_value="products",
                    width="100%",
                ),
                spacing="4",
                width="100%",
                padding="1.2em",
                max_width="72rem",
                margin_x="auto",
            ),
            width="100%",
        ),
        spacing="0",
        width="100%",
    )


def login_view() -> rx.Component:
    return rx.center(
        rx.card(
            rx.vstack(
                rx.box(
                    rx.icon("store", size=26, color=rx.color("accent", 9)),
                    background_color=rx.color("accent", 3),
                    border_radius="12px",
                    padding="0.6em",
                ),
                rx.heading("Bondom Account", size="6"),
                rx.text("Admin sign in", size="2", color_scheme="gray"),
                rx.input(
                    placeholder="Admin password",
                    type="password",
                    value=AdminState.password_input,
                    on_change=AdminState.set_password_input,
                    width="100%",
                    size="3",
                ),
                rx.button(
                    rx.icon("lock-open", size=16),
                    "Sign in",
                    on_click=AdminState.login,
                    width="100%",
                    size="3",
                ),
                rx.cond(
                    AdminState.login_message != "",
                    rx.callout(
                        AdminState.login_message,
                        icon="triangle-alert",
                        color_scheme="red",
                        size="1",
                        width="100%",
                    ),
                ),
                spacing="4",
                width="20em",
                align="center",
            ),
            size="4",
        ),
        height="90vh",
    )


@rx.page(route="/", title="Bondom Account — Admin", on_load=AdminState.load_all)
def index() -> rx.Component:
    return rx.cond(AdminState.authed, dashboard_view(), login_view())


app = rx.App(
    theme=rx.theme(
        accent_color="indigo",
        gray_color="slate",
        radius="large",
    )
)
