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

    product_options: list[str] = []
    selected_product: str = ""
    upload_message: str = ""
    bot_show_stock: bool = True
    orders_message: str = ""
    announcement_text: str = ""
    announcement_message: str = ""
    users_message: str = ""
    user_adjust_amount: str = ""

    # Add-product form
    new_name: str = ""
    new_price: str = ""
    new_category: str = ""
    new_warranty: str = "0"
    form_message: str = ""

    # Manage-selected-product form
    manage_name: str = ""
    manage_price: str = ""
    manage_warranty: str = "0"
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
        if self.selected_product and self.selected_product in self.product_options:
            selected_id = int(self.selected_product.split(" — ", 1)[0])
            selected = next(
                (p for p in self.products if p.id == selected_id), None
            )
            if selected is not None:
                self.manage_name = selected.name
                self.manage_price = selected.price
                self.manage_warranty = str(selected.warranty_days)
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

    # ----------------------------------------------------------------- #
    # Add product
    # ----------------------------------------------------------------- #
    def set_new_name(self, v: str) -> None:
        self.new_name = v

    def set_new_price(self, v: str) -> None:
        self.new_price = v

    def set_new_category(self, v: str) -> None:
        self.new_category = v

    def set_new_warranty(self, v: str) -> None:
        self.new_warranty = v

    async def add_product(self) -> None:
        if not self.authed:
            return
        try:
            payload = ProductCreate(
                name=self.new_name.strip(),
                price=Decimal(self.new_price or "0"),
                category=self.new_category.strip() or "general",
                warranty_days=int(self.new_warranty or "0"),
            )
        except (InvalidOperation, ValueError) as exc:
            self.form_message = f"⚠ Invalid input: {exc}"
            return

        async with AsyncSessionLocal() as session:
            product = await services.create_product(session, payload)
        self.form_message = f"✅ Added '{product.name}' (#{product.id})."
        self.new_name = self.new_price = self.new_category = ""
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
    def set_product(self, value: str) -> None:
        self.selected_product = value
        selected_id = int(value.split(" — ", 1)[0])
        selected = next((p for p in self.products if p.id == selected_id), None)
        self.manage_name = selected.name if selected is not None else ""
        self.manage_price = selected.price if selected is not None else ""
        self.manage_warranty = (
            str(selected.warranty_days) if selected is not None else "0"
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
def stat_card(label: str, value, accent: str = "gray") -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.text(label, size="1", color_scheme="gray"),
            rx.heading(value, size="6", color_scheme=accent),
            spacing="1",
        ),
        flex="1",
    )


def kpi_row() -> rx.Component:
    return rx.hstack(
        stat_card("Users", AdminState.stat_users, "blue"),
        stat_card("Orders", AdminState.stat_orders, "purple"),
        stat_card("Paid orders", AdminState.stat_paid, "green"),
        stat_card("Revenue ($)", AdminState.stat_revenue, "green"),
        stat_card("Stock left", AdminState.stat_stock, "orange"),
        spacing="3",
        width="100%",
    )


def add_product_form() -> rx.Component:
    return rx.vstack(
        rx.heading("Add Product", size="5"),
        rx.hstack(
            rx.input(
                placeholder="Name",
                value=AdminState.new_name,
                on_change=AdminState.set_new_name,
            ),
            rx.input(
                placeholder="Price (USD)",
                type="number",
                value=AdminState.new_price,
                on_change=AdminState.set_new_price,
                width="8em",
            ),
            rx.input(
                placeholder="Category",
                value=AdminState.new_category,
                on_change=AdminState.set_new_category,
            ),
            rx.input(
                placeholder="Warranty days",
                type="number",
                value=AdminState.new_warranty,
                on_change=AdminState.set_new_warranty,
                width="8em",
            ),
            rx.button("Add", on_click=AdminState.add_product),
            spacing="2",
            wrap="wrap",
            align="center",
        ),
        rx.text(AdminState.form_message, color_scheme="gray"),
        width="100%",
        spacing="2",
    )


def bot_settings_card() -> rx.Component:
    return rx.vstack(
        rx.heading("Bot Settings", size="5"),
        rx.hstack(
            rx.text("Show exact stock count in bot product list"),
            rx.switch(
                checked=AdminState.bot_show_stock,
                on_change=AdminState.set_bot_show_stock_toggle,
            ),
            spacing="3",
            align="center",
        ),
        width="100%",
        spacing="2",
    )


def announcement_card() -> rx.Component:
    return rx.vstack(
        rx.heading("Announcement", size="5"),
        rx.text(
            "Broadcast a message to all active Telegram clients.",
            color_scheme="gray",
        ),
        rx.text_area(
            placeholder="Write announcement for clients...",
            value=AdminState.announcement_text,
            on_change=AdminState.set_announcement_text,
            width="100%",
            min_height="8em",
        ),
        rx.button(
            "Publish Announcement",
            color_scheme="blue",
            on_click=AdminState.publish_announcement,
        ),
        rx.text(AdminState.announcement_message),
        width="100%",
        spacing="2",
    )


def _product_row(p: ProductRow) -> rx.Component:
    return rx.table.row(
        rx.table.cell(p.id),
        rx.table.cell(p.name),
        rx.table.cell("$" + p.price),
        rx.table.cell(p.category),
        rx.table.cell(rx.badge(p.available, color_scheme="orange")),
        rx.table.cell(rx.badge(p.sold, color_scheme="green")),
        rx.table.cell("$" + p.revenue),
        rx.table.cell(
            rx.switch(
                checked=p.is_active,
                on_change=lambda checked: AdminState.toggle_product(p.id, checked),
            )
        ),
        rx.table.cell(
            rx.button(
                "Clear stock",
                size="1",
                color_scheme="red",
                variant="soft",
                on_click=lambda: AdminState.clear_stock(p.id),
            )
        ),
    )


def products_table() -> rx.Component:
    return rx.vstack(
        rx.heading("Products & Stock", size="5"),
        rx.text(
            "Available = ready to sell (before buy) · Sold = allocated to "
            "orders (after buy).",
            size="1",
            color_scheme="gray",
        ),
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
                    rx.table.column_header_cell("Actions"),
                )
            ),
            rx.table.body(rx.foreach(AdminState.products, _product_row)),
            width="100%",
        ),
        width="100%",
        spacing="2",
    )


def _order_row(o: OrderRow) -> rx.Component:
    return rx.table.row(
        rx.table.cell(o.id),
        rx.table.cell(o.buyer),
        rx.table.cell("$" + o.total_price),
        rx.table.cell(rx.badge(o.status)),
        rx.table.cell(o.created_at),
    )


def orders_table() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.heading("Orders", size="5"),
            rx.spacer(),
            rx.button(
                "Clear Orders Now",
                color_scheme="red",
                variant="soft",
                on_click=AdminState.clear_orders_now,
            ),
            width="100%",
            align="center",
        ),
        rx.text(AdminState.orders_message),
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
            rx.table.body(rx.foreach(AdminState.orders, _order_row)),
            width="100%",
        ),
        width="100%",
        spacing="2",
    )


def _user_row(u: UserRow) -> rx.Component:
    return rx.table.row(
        rx.table.cell(u.id),
        rx.table.cell(u.telegram_id),
        rx.table.cell(u.username),
        rx.table.cell("$" + u.balance),
        rx.table.cell(
            rx.badge(
                rx.cond(u.is_blocked, "Blocked", "Allowed"),
                color_scheme=rx.cond(u.is_blocked, "red", "green"),
            )
        ),
        rx.table.cell(
            rx.switch(
                checked=u.is_active,
                on_change=lambda checked: AdminState.toggle_user(u.id, checked),
            )
        ),
        rx.table.cell(
            rx.cond(
                u.is_blocked,
                rx.button(
                    "Unblock",
                    size="1",
                    color_scheme="green",
                    variant="solid",
                    on_click=lambda: AdminState.unblock_user(u.id),
                ),
                rx.button(
                    "Block Forever",
                    size="1",
                    color_scheme="red",
                    variant="solid",
                    on_click=lambda: AdminState.block_user_forever(u.id),
                ),
            )
        ),
        rx.table.cell(
            rx.hstack(
                rx.button(
                    "+",
                    size="1",
                    color_scheme="green",
                    on_click=lambda: AdminState.credit_user_wallet(u.id),
                ),
                rx.button(
                    "-",
                    size="1",
                    color_scheme="orange",
                    on_click=lambda: AdminState.debit_user_wallet(u.id),
                ),
                spacing="2",
            )
        ),
    )


def users_table() -> rx.Component:
    return rx.vstack(
        rx.heading("Users", size="5"),
        rx.hstack(
            rx.text("Wallet adjust amount (USD):"),
            rx.input(
                placeholder="e.g. 5.00",
                value=AdminState.user_adjust_amount,
                on_change=AdminState.set_user_adjust_amount,
                width="10em",
            ),
            spacing="2",
            align="center",
        ),
        rx.text(AdminState.users_message),
        rx.table.root(
            rx.table.header(
                rx.table.row(
                    rx.table.column_header_cell("ID"),
                    rx.table.column_header_cell("Telegram ID"),
                    rx.table.column_header_cell("Username"),
                    rx.table.column_header_cell("Wallet"),
                    rx.table.column_header_cell("Security Status"),
                    rx.table.column_header_cell("Active (toggle to suspend)"),
                    rx.table.column_header_cell("Permanent Block"),
                    rx.table.column_header_cell("Wallet +/-"),
                )
            ),
            rx.table.body(rx.foreach(AdminState.users, _user_row)),
            width="100%",
        ),
        width="100%",
        spacing="2",
    )


def bulk_upload_card() -> rx.Component:
    return rx.vstack(
        rx.heading("Bulk Upload Inventory", size="5"),
        rx.text(
            "Pick a product, then upload a .txt file with one item "
            "(credential / key) per line.",
            color_scheme="gray",
        ),
        rx.select(
            AdminState.product_options,
            value=AdminState.selected_product,
            on_change=AdminState.set_product,
            placeholder="Select product…",
            width="20em",
        ),
        rx.upload(
            rx.text("📄 Drop a .txt file here or click to browse"),
            id="bulk_inventory",
            max_files=5,
            border="1px dashed var(--accent-8)",
            padding="2em",
            width="100%",
        ),
        rx.hstack(rx.foreach(rx.selected_files("bulk_inventory"), rx.text)),
        rx.button(
            "Bulk Upload",
            on_click=AdminState.handle_upload(
                rx.upload_files(upload_id="bulk_inventory")
            ),
        ),
        rx.text(AdminState.upload_message),
        width="100%",
        spacing="3",
    )


def manage_selected_product_card() -> rx.Component:
    return rx.vstack(
        rx.heading("Manage Current Product", size="5"),
        rx.text(
            "Rename, add stock to current product, or delete product "
            "(delete is immediate).",
            color_scheme="gray",
        ),
        rx.select(
            AdminState.product_options,
            value=AdminState.selected_product,
            on_change=AdminState.set_product,
            placeholder="Select product…",
            width="20em",
        ),
        rx.hstack(
            rx.input(
                placeholder="New product name",
                value=AdminState.manage_name,
                on_change=AdminState.set_manage_name,
                width="24em",
            ),
            rx.button("Rename", on_click=AdminState.rename_selected_product),
            spacing="2",
            wrap="wrap",
        ),
        rx.hstack(
            rx.input(
                placeholder="Price (USD)",
                type="number",
                value=AdminState.manage_price,
                on_change=AdminState.set_manage_price,
                width="12em",
            ),
            rx.button("Update Price", on_click=AdminState.update_selected_price),
            spacing="2",
            wrap="wrap",
        ),
        rx.hstack(
            rx.input(
                placeholder="Warranty days (0 = no warranty)",
                type="number",
                value=AdminState.manage_warranty,
                on_change=AdminState.set_manage_warranty,
                width="18em",
            ),
            rx.button(
                "Update Warranty",
                on_click=AdminState.update_selected_warranty,
            ),
            spacing="2",
            wrap="wrap",
        ),
        rx.text_area(
            placeholder=(
                "Optional note for client delivery (leave blank for no note)"
            ),
            value=AdminState.manage_client_note,
            on_change=AdminState.set_manage_client_note,
            width="100%",
            min_height="6em",
        ),
        rx.button("Save Client Note", on_click=AdminState.save_client_note),
        rx.text_area(
            placeholder=(
                "Add stock lines here (one credential/key per line), "
                "then click Add Stock"
            ),
            value=AdminState.manage_stock_lines,
            on_change=AdminState.set_manage_stock_lines,
            width="100%",
            min_height="10em",
        ),
        rx.hstack(
            rx.button(
                "Add Stock",
                color_scheme="green",
                on_click=AdminState.add_stock_to_selected,
            ),
            rx.button(
                "Delete Product",
                color_scheme="red",
                variant="soft",
                on_click=AdminState.delete_selected_product,
            ),
            spacing="2",
            wrap="wrap",
        ),
        rx.text(AdminState.manage_message),
        width="100%",
        spacing="3",
    )


def login_view() -> rx.Component:
    return rx.center(
        rx.vstack(
            rx.heading("🔐 Bondom Account — Admin", size="6"),
            rx.input(
                placeholder="Admin password",
                type="password",
                value=AdminState.password_input,
                on_change=AdminState.set_password_input,
                width="100%",
            ),
            rx.button("Sign in", on_click=AdminState.login, width="100%"),
            rx.cond(
                AdminState.login_message != "",
                rx.text(AdminState.login_message, color_scheme="red"),
            ),
            spacing="4",
            width="20em",
        ),
        height="80vh",
    )


def dashboard_view() -> rx.Component:
    return rx.container(
        rx.vstack(
            rx.hstack(
                rx.heading("🛍 Bondom Account — Admin", size="7"),
                rx.spacer(),
                rx.button("↻ Refresh", on_click=AdminState.load_all),
                rx.button(
                    "Sign out",
                    on_click=AdminState.logout,
                    variant="soft",
                    color_scheme="gray",
                ),
                width="100%",
                align="center",
            ),
            rx.divider(),
            kpi_row(),
            rx.divider(),
            bot_settings_card(),
            rx.divider(),
            announcement_card(),
            rx.divider(),
            add_product_form(),
            rx.divider(),
            products_table(),
            rx.divider(),
            manage_selected_product_card(),
            rx.divider(),
            bulk_upload_card(),
            rx.divider(),
            orders_table(),
            rx.divider(),
            users_table(),
            spacing="6",
            padding_y="2em",
        ),
        size="4",
    )


@rx.page(route="/", title="Bondom Account — Admin", on_load=AdminState.load_all)
def index() -> rx.Component:
    return rx.cond(AdminState.authed, dashboard_view(), login_view())


app = rx.App()
