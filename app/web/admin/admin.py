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

import asyncio
import dataclasses
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import reflex as rx
from aiogram import Bot

from shared import admin_auth, audit, services
from shared.config import settings
from shared.database import AsyncSessionLocal
from shared.schemas import ProductCreate

# Sign the admin out after this long without interaction. Kept short
# because the panel can move money (balance adjustments, refunds).
SESSION_IDLE_SECONDS = 15 * 60
# How often the background watchdog re-checks idle time.
SESSION_TICK_SECONDS = 30


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
class AuditRow:
    id: int
    actor: str
    action: str
    target: str
    summary: str
    reason: str
    created_at: str


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

    # Session lifetime
    last_activity: float = 0.0
    session_notice: str = ""
    # True while the .env bootstrap password is still in use, so the UI
    # can nudge the admin to set a real one.
    using_env_password: bool = False

    # Change-password form
    current_password: str = ""
    new_password: str = ""
    confirm_password: str = ""
    password_message: str = ""
    password_ok: bool = False
    password_score: int = 0
    password_strength_label: str = ""

    # Table sorting — column key + direction per table
    product_sort: str = "id"
    product_desc: bool = False
    order_sort: str = "id"
    order_desc: bool = True
    user_sort: str = "id"
    user_desc: bool = False

    # Filters
    order_status_filter: str = "all"
    user_status_filter: str = "all"

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

    # Per-user wallet dialog
    wallet_user_id: int = 0
    wallet_username: str = ""
    wallet_balance: str = "0.00"
    wallet_message: str = ""
    wallet_ok: bool = False

    # Delete-user dialog
    del_user_id: int = 0
    del_username: str = ""
    del_telegram_id: str = ""
    del_orders: int = 0
    del_sms: int = 0
    del_topups: int = 0
    del_balance: str = "0.00"
    del_can_delete: bool = False
    del_reason: str = ""
    del_message: str = ""
    del_done: bool = False
    del_reason_input: str = ""
    del_is_deleted: bool = False

    # Activity log
    audit_rows: list[AuditRow] = []
    audit_filter: str = "all"
    audit_search: str = ""

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

    def set_audit_search(self, v: str) -> None:
        self.audit_search = v

    @rx.var
    def filtered_audit(self) -> list[AuditRow]:
        q = self.audit_search.strip().lower()
        if not q:
            return self.audit_rows
        return [
            e
            for e in self.audit_rows
            if q in e.action.lower()
            or q in e.summary.lower()
            or q in e.reason.lower()
            or q in e.target.lower()
        ]

    @rx.var
    def audit_result_count(self) -> int:
        return len(self.filtered_audit)

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

    # Sorting helpers -------------------------------------------------
    # Numeric columns are stored as strings (state must serialise), so
    # they are coerced back to floats for comparison — otherwise "10"
    # sorts before "9".
    @staticmethod
    def _num(value: str) -> float:
        try:
            return float(str(value).replace("$", "").replace(",", "").strip())
        except (TypeError, ValueError):
            return 0.0

    def sort_products(self, column: str) -> None:
        self.product_desc = (
            not self.product_desc if self.product_sort == column else False
        )
        self.product_sort = column

    def sort_orders(self, column: str) -> None:
        self.order_desc = (
            not self.order_desc if self.order_sort == column else False
        )
        self.order_sort = column

    def sort_users(self, column: str) -> None:
        self.user_desc = (
            not self.user_desc if self.user_sort == column else False
        )
        self.user_sort = column

    def set_order_status_filter(self, v: str) -> None:
        self.order_status_filter = v

    def set_user_status_filter(self, v: str) -> None:
        self.user_status_filter = v

    @rx.var
    def filtered_products(self) -> list[ProductRow]:
        q = self.product_search.strip().lower()
        rows = self.products
        if q:
            rows = [
                p
                for p in rows
                if q in p.name.lower()
                or q in p.category.lower()
                or q == str(p.id)
            ]

        keys = {
            "id": lambda p: p.id,
            "name": lambda p: p.name.lower(),
            "category": lambda p: p.category.lower(),
            "price": lambda p: self._num(p.price),
            "available": lambda p: p.available,
            "sold": lambda p: p.sold,
            "revenue": lambda p: self._num(p.revenue),
        }
        key = keys.get(self.product_sort, keys["id"])
        return sorted(rows, key=key, reverse=self.product_desc)

    @rx.var
    def filtered_orders(self) -> list[OrderRow]:
        q = self.order_search.strip().lower()
        rows = self.orders

        if self.order_status_filter != "all":
            rows = [
                o
                for o in rows
                if o.status.lower() == self.order_status_filter
            ]
        if q:
            rows = [
                o
                for o in rows
                if q in o.buyer.lower()
                or q in o.status.lower()
                or q == str(o.id)
            ]

        keys = {
            "id": lambda o: o.id,
            "buyer": lambda o: o.buyer.lower(),
            "total": lambda o: self._num(o.total_price),
            "status": lambda o: o.status.lower(),
            "created": lambda o: o.created_at,
        }
        key = keys.get(self.order_sort, keys["id"])
        return sorted(rows, key=key, reverse=self.order_desc)

    @rx.var
    def order_status_options(self) -> list[str]:
        return ["all"] + sorted({o.status.lower() for o in self.orders})

    @rx.var
    def filtered_users(self) -> list[UserRow]:
        q = self.user_search.strip().lower()
        rows = self.users

        if self.user_status_filter == "active":
            rows = [u for u in rows if u.is_active and not u.is_blocked]
        elif self.user_status_filter == "suspended":
            rows = [u for u in rows if not u.is_active]
        elif self.user_status_filter == "blocked":
            rows = [u for u in rows if u.is_blocked]

        if q:
            rows = [
                u
                for u in rows
                if q in u.username.lower()
                or q in u.telegram_id
                or q == str(u.id)
            ]

        keys = {
            "id": lambda u: u.id,
            "username": lambda u: u.username.lower(),
            "telegram": lambda u: u.telegram_id,
            "balance": lambda u: self._num(u.balance),
        }
        key = keys.get(self.user_sort, keys["id"])
        return sorted(rows, key=key, reverse=self.user_desc)

    # ----------------------------------------------------------------- #
    # Analytics — derived from rows already in state, so charts cost no
    # extra queries and stay consistent with the tables below them.
    # ----------------------------------------------------------------- #
    @rx.var
    def revenue_by_product(self) -> list[dict]:
        """Top products by revenue, for the dashboard bar chart."""
        rows = [
            {"name": p.name[:18], "revenue": round(self._num(p.revenue), 2)}
            for p in self.products
            if self._num(p.revenue) > 0
        ]
        rows.sort(key=lambda r: r["revenue"], reverse=True)
        return rows[:7]

    @rx.var
    def orders_by_status(self) -> list[dict]:
        counts: dict[str, int] = {}
        for o in self.orders:
            counts[o.status.lower()] = counts.get(o.status.lower(), 0) + 1
        palette = {
            "delivered": "#10b981",
            "paid": "#6366f1",
            "pending": "#f59e0b",
            "canceled": "#ef4444",
        }
        return [
            {"name": k, "value": v, "fill": palette.get(k, "#94a3b8")}
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        ]

    @rx.var
    def orders_by_day(self) -> list[dict]:
        """Order volume per calendar day, oldest first (last 14 days)."""
        counts: dict[str, int] = {}
        for o in self.orders:
            day = (o.created_at or "")[:10]
            if day:
                counts[day] = counts.get(day, 0) + 1
        days = sorted(counts)[-14:]
        return [{"day": d[5:], "orders": counts[d]} for d in days]

    @rx.var
    def stock_alerts(self) -> list[ProductRow]:
        """Active products that are out of, or nearly out of, stock."""
        return [
            p
            for p in self.products
            if p.is_active and p.available <= 3
        ]

    @rx.var
    def stock_alert_count(self) -> int:
        return len(self.stock_alerts)

    # Result counters, so tables can show "showing X of Y".
    @rx.var
    def product_result_count(self) -> int:
        return len(self.filtered_products)

    @rx.var
    def order_result_count(self) -> int:
        return len(self.filtered_orders)

    @rx.var
    def user_result_count(self) -> int:
        return len(self.filtered_users)

    @rx.var
    def sms_result_count(self) -> int:
        return len(self.filtered_sms)

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

    def touch(self) -> None:
        """Record interaction so the idle watchdog holds off."""
        self.last_activity = time.time()

    @rx.var
    def idle_seconds_left(self) -> int:
        if not self.authed:
            return 0
        left = SESSION_IDLE_SECONDS - (time.time() - self.last_activity)
        return max(0, int(left))

    async def login(self):
        password = self.password_input
        self.password_input = ""

        if not password:
            self.login_message = "Enter your password."
            return

        async with AsyncSessionLocal() as session:
            ok = await admin_auth.verify_admin_password(session, password)
            bootstrap = not await admin_auth.has_stored_password(session)

        if not ok:
            # Deliberately vague, and slow enough to blunt brute force
            # against a panel that is exposed to the internet.
            await asyncio.sleep(0.6)
            self.login_message = "Incorrect password."
            return

        if bootstrap and not settings.admin_password:
            self.login_message = (
                "ADMIN_PASSWORD is not set in .env on the server."
            )
            return

        self.authed = True
        self.login_message = ""
        self.session_notice = ""
        self.using_env_password = bootstrap
        self.touch()
        await self.load_all()
        return AdminState.session_watchdog

    def logout(self, notice: str = "") -> None:
        """Drop the session and wipe anything sensitive held in state."""
        self.authed = False
        self.session_notice = notice
        self.password_input = ""
        self.current_password = ""
        self.new_password = ""
        self.confirm_password = ""
        self.password_message = ""
        self.password_ok = False

    @rx.event(background=True)
    async def session_watchdog(self):
        """Sign the admin out after a period with no interaction.

        Runs server-side, so closing the laptop lid or leaving the tab
        open does not keep the session alive.
        """
        while True:
            await asyncio.sleep(SESSION_TICK_SECONDS)
            async with self:
                if not self.authed:
                    return
                idle = time.time() - self.last_activity
                if idle >= SESSION_IDLE_SECONDS:
                    self.logout(
                        "Signed out automatically after "
                        f"{SESSION_IDLE_SECONDS // 60} minutes of inactivity."
                    )
                    return

    # ------------------------------------------------------------- #
    # Change password
    # ------------------------------------------------------------- #
    def set_current_password(self, v: str) -> None:
        self.current_password = v

    def set_new_password(self, v: str) -> None:
        self.new_password = v
        score, label = admin_auth.password_strength(v)
        self.password_score = score
        self.password_strength_label = label

    def set_confirm_password(self, v: str) -> None:
        self.confirm_password = v

    async def change_password(self):
        if not self.authed:
            return
        self.touch()

        check = admin_auth.validate_new_password(
            self.new_password, self.confirm_password
        )
        if not check.ok:
            self.password_ok = False
            self.password_message = check.message
            return

        if self.new_password == self.current_password:
            self.password_ok = False
            self.password_message = "The new password matches the old one."
            return

        async with AsyncSessionLocal() as session:
            if not await admin_auth.verify_admin_password(
                session, self.current_password
            ):
                await asyncio.sleep(0.6)
                self.password_ok = False
                self.password_message = "Current password is incorrect."
                return
            await admin_auth.set_admin_password(session, self.new_password)

        self.current_password = ""
        self.new_password = ""
        self.confirm_password = ""
        self.password_score = 0
        self.password_strength_label = ""
        self.using_env_password = False
        self.password_ok = True
        self.password_message = (
            "Password updated. The previous password no longer works."
        )
        await audit.log_action(
            audit.ACTION_PASSWORD_CHANGE,
            summary="Admin password changed",
        )
        return rx.toast.success("Admin password changed")

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
            entries = await audit.list_entries(session, limit=400)
        self.audit_rows = [
            AuditRow(
                id=e.id,
                actor=e.actor,
                action=e.action,
                target=e.target,
                summary=e.summary,
                reason=e.reason,
                created_at=e.created_at,
            )
            for e in entries
        ]

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
        self.wallet_message = ""

    async def open_wallet_dialog(self, user_id: int) -> None:
        """Open the per-user wallet dialog with that user's live balance.

        The amount used to live in one shared box at the top of the card
        while the +/- buttons sat in each row, so it was easy to click
        without an amount (nothing happened) or to adjust the wrong user.
        Everything now happens in one dialog scoped to a single user.
        """
        if not self.authed:
            return
        self.touch()
        row = next((u for u in self.users if u.id == user_id), None)
        self.wallet_user_id = user_id
        self.wallet_username = row.username if row else f"#{user_id}"
        async with AsyncSessionLocal() as session:
            balance = await services.get_user_balance(session, user_id)
        self.wallet_balance = f"{balance:.2f}"
        self.user_adjust_amount = ""
        self.wallet_message = ""
        self.wallet_ok = False

    def close_wallet_dialog(self) -> None:
        self.wallet_message = ""
        self.user_adjust_amount = ""

    # ------------------------------------------------------------- #
    # Delete user
    # ------------------------------------------------------------- #
    async def open_delete_dialog(self, user_id: int) -> None:
        """Load what deleting this user would destroy, before confirming."""
        if not self.authed:
            return
        self.touch()
        self.del_message = ""
        self.del_done = False
        self.del_reason_input = ""
        async with AsyncSessionLocal() as session:
            impact = await services.get_user_delete_impact(session, user_id)

        self.del_user_id = impact.user_id
        self.del_username = impact.username or f"#{impact.user_id}"
        self.del_telegram_id = impact.telegram_id
        self.del_orders = impact.orders
        self.del_sms = impact.sms_orders
        self.del_topups = impact.topups
        self.del_balance = f"{impact.balance:.2f}"
        self.del_can_delete = impact.can_delete
        self.del_reason = impact.blocked_reason
        async with AsyncSessionLocal() as session:
            self.del_is_deleted = await services.is_user_deleted(
                session, user_id
            )

    def set_del_reason(self, v: str) -> None:
        self.del_reason_input = v

    def close_delete_dialog(self) -> None:
        self.del_message = ""
        self.del_done = False
        self.del_reason_input = ""

    async def soft_delete_user(self):
        """Deactivate the account but keep everything, so it can come back."""
        if not self.authed:
            return
        self.touch()
        user_id = self.del_user_id
        username = self.del_username
        reason = self.del_reason_input.strip()

        async with AsyncSessionLocal() as session:
            await services.soft_delete_user(session, user_id, reason)

        await audit.log_action(
            audit.ACTION_USER_SOFT_DELETE,
            target_type="user",
            target_id=user_id,
            summary=f"Deactivated {username} (recoverable)",
            reason=reason,
        )
        self.del_done = True
        self.del_is_deleted = True
        self.del_message = (
            f"{username} was deactivated. Nothing was destroyed — you can "
            "restore this account at any time."
        )
        await self.load_all()
        return rx.toast.success(f"Deactivated {username}")

    async def restore_user(self):
        if not self.authed:
            return
        self.touch()
        user_id = self.del_user_id
        username = self.del_username

        async with AsyncSessionLocal() as session:
            await services.restore_user(session, user_id)

        await audit.log_action(
            audit.ACTION_USER_RESTORE,
            target_type="user",
            target_id=user_id,
            summary=f"Restored {username}",
            reason=self.del_reason_input.strip(),
        )
        self.del_done = True
        self.del_is_deleted = False
        self.del_message = f"{username} was restored and can sign in again."
        await self.load_all()
        return rx.toast.success(f"Restored {username}")

    async def confirm_delete_user(self):
        if not self.authed:
            return
        self.touch()
        user_id = self.del_user_id
        username = self.del_username

        try:
            async with AsyncSessionLocal() as session:
                await services.delete_user(session, user_id)
        except services.UserHasOrdersError as exc:
            # Re-checked server-side, so a stale dialog cannot force it.
            self.del_can_delete = False
            self.del_message = str(exc)
            return
        except services.UserNotFoundError:
            self.del_message = "That user no longer exists."
            self.del_done = True
            await self.load_all()
            return

        await audit.log_action(
            audit.ACTION_USER_DELETE,
            target_type="user",
            target_id=user_id,
            summary=(
                f"Deleted {username} (telegram {self.del_telegram_id}); "
                f"removed {self.del_sms} SMS order(s), "
                f"{self.del_topups} top-up(s), wallet ${self.del_balance}"
            ),
            reason=self.del_reason_input.strip(),
        )

        self.del_done = True
        self.del_message = f"{username} was permanently deleted."
        await self.load_all()
        return rx.toast.success(f"Deleted {username}")

    def _wallet_amount(self) -> Decimal | None:
        """Parse the entered amount, setting an error message if invalid."""
        raw = (self.user_adjust_amount or "").strip()
        if not raw:
            self.wallet_ok = False
            self.wallet_message = "Enter an amount first."
            return None
        try:
            amount = Decimal(raw).quantize(Decimal("0.01"))
        except InvalidOperation:
            self.wallet_ok = False
            self.wallet_message = "That is not a valid amount."
            return None
        if amount <= 0:
            self.wallet_ok = False
            self.wallet_message = "Amount must be greater than 0."
            return None
        return amount

    async def credit_user_wallet(self):
        if not self.authed:
            return
        self.touch()
        amount = self._wallet_amount()
        if amount is None:
            return

        user_id = self.wallet_user_id
        async with AsyncSessionLocal() as session:
            updated = await services.adjust_user_balance(session, user_id, amount)

        self.wallet_balance = f"{updated:.2f}"
        self.wallet_ok = True
        self.wallet_message = (
            f"Credited ${amount:.2f}. New balance ${updated:.2f}."
        )
        self.user_adjust_amount = ""
        await audit.log_action(
            audit.ACTION_WALLET_CREDIT,
            target_type="user",
            target_id=user_id,
            summary=(
                f"Credited ${amount:.2f} to {self.wallet_username}; "
                f"new balance ${updated:.2f}"
            ),
        )
        await self.load_all()
        return rx.toast.success(
            f"Credited ${amount:.2f} to {self.wallet_username}"
        )

    async def debit_user_wallet(self):
        if not self.authed:
            return
        self.touch()
        amount = self._wallet_amount()
        if amount is None:
            return

        user_id = self.wallet_user_id
        try:
            async with AsyncSessionLocal() as session:
                updated = await services.adjust_user_balance(
                    session, user_id, Decimal("0") - amount
                )
        except services.InsufficientBalanceError as exc:
            self.wallet_ok = False
            self.wallet_message = (
                f"Not enough balance — needs ${exc.required:.2f}, "
                f"has ${exc.balance:.2f}."
            )
            return

        self.wallet_balance = f"{updated:.2f}"
        self.wallet_ok = True
        self.wallet_message = (
            f"Debited ${amount:.2f}. New balance ${updated:.2f}."
        )
        self.user_adjust_amount = ""
        await audit.log_action(
            audit.ACTION_WALLET_DEBIT,
            target_type="user",
            target_id=user_id,
            summary=(
                f"Debited ${amount:.2f} from {self.wallet_username}; "
                f"new balance ${updated:.2f}"
            ),
        )
        await self.load_all()
        return rx.toast.success(
            f"Debited ${amount:.2f} from {self.wallet_username}"
        )

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
        width=rx.breakpoints(initial="100%", sm="16em"),
        size="2",
        variant="surface",
    )


def confirm_action(
    trigger_label: str,
    icon_name: str,
    title: str,
    description,
    confirm_label: str,
    on_confirm,
    color: str = "red",
) -> rx.Component:
    """Destructive action behind an explicit confirmation dialog.

    Anything that destroys data or money (clearing stock, permanent
    blocks) goes through this rather than firing straight off a click.
    """
    return rx.alert_dialog.root(
        rx.alert_dialog.trigger(
            rx.button(
                rx.icon(icon_name, size=14),
                trigger_label,
                size="1",
                color_scheme=color,
                variant="soft",
            )
        ),
        rx.alert_dialog.content(
            rx.alert_dialog.title(title),
            rx.alert_dialog.description(description, size="2"),
            rx.hstack(
                rx.alert_dialog.cancel(
                    rx.button("Cancel", variant="soft", color_scheme="gray")
                ),
                rx.alert_dialog.action(
                    rx.button(
                        confirm_label, color_scheme=color, on_click=on_confirm
                    )
                ),
                spacing="3",
                justify="end",
                margin_top="1.2em",
            ),
            max_width="26em",
        ),
    )


def sortable_header(label: str, column: str, current, descending, on_sort):
    """Table header cell that toggles sort direction when clicked."""
    return rx.table.column_header_cell(
        rx.hstack(
            rx.text(label, size="1", weight="bold"),
            rx.cond(
                current == column,
                rx.icon(
                    rx.cond(descending, "arrow-down", "arrow-up"),
                    size=13,
                    color=rx.color("accent", 9),
                ),
                rx.icon("chevrons-up-down", size=13, opacity=0.35),
            ),
            spacing="1",
            align="center",
        ),
        on_click=on_sort(column),
        cursor="pointer",
        _hover={"background_color": rx.color("gray", 3)},
        white_space="nowrap",
    )


def result_count(shown, total_label: str) -> rx.Component:
    return rx.text(
        f"{shown} {total_label}",
        size="1",
        color_scheme="gray",
        white_space="nowrap",
    )


def chart_card(title: str, subtitle: str, body: rx.Component) -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.vstack(
                rx.heading(title, size="3"),
                rx.text(subtitle, size="1", color_scheme="gray"),
                spacing="0",
                align="start",
            ),
            body,
            spacing="3",
            width="100%",
        ),
        size="2",
        width="100%",
    )


def empty_chart(message: str) -> rx.Component:
    return rx.center(
        rx.vstack(
            rx.icon("chart-no-axes-column", size=22, opacity=0.4),
            rx.text(message, size="1", color_scheme="gray"),
            spacing="2",
            align="center",
        ),
        height="12em",
        width="100%",
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
            confirm_action(
                "Clear stock",
                "eraser",
                "Clear unsold stock?",
                rx.fragment(
                    "This permanently deletes every unsold inventory item for ",
                    rx.text.strong(p.name),
                    f" ({p.available} available). Items already sold are kept. "
                    "This cannot be undone.",
                ),
                "Delete unsold stock",
                lambda: AdminState.clear_stock(p.id),
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
                            *[
                                sortable_header(
                                    label,
                                    col,
                                    AdminState.product_sort,
                                    AdminState.product_desc,
                                    AdminState.sort_products,
                                )
                                for label, col in [
                                    ("ID", "id"),
                                    ("Name", "name"),
                                    ("Price", "price"),
                                    ("Category", "category"),
                                    ("Available", "available"),
                                    ("Sold", "sold"),
                                    ("Revenue", "revenue"),
                                ]
                            ],
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
                            *[
                                sortable_header(
                                    label,
                                    col,
                                    AdminState.order_sort,
                                    AdminState.order_desc,
                                    AdminState.sort_orders,
                                )
                                for label, col in [
                                    ("ID", "id"),
                                    ("Buyer", "buyer"),
                                    ("Total", "total"),
                                    ("Status", "status"),
                                    ("Created", "created"),
                                ]
                            ],
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
                confirm_action(
                    "Block Forever",
                    "ban",
                    "Block this user permanently?",
                    rx.fragment(
                        rx.text.strong(u.username),
                        " will be blocked from buying anywhere — bot and "
                        "website. You can unblock them again later.",
                    ),
                    "Block user",
                    lambda: AdminState.block_user_forever(u.id),
                ),
            )
        ),
        rx.table.cell(
            rx.hstack(
                wallet_dialog(u),
                delete_user_dialog(u),
                spacing="2",
            )
        ),
        align="center",
    )


def _impact_row(label: str, value, danger: bool = False) -> rx.Component:
    return rx.hstack(
        rx.text(label, size="2", color_scheme="gray"),
        rx.spacer(),
        rx.text(
            value,
            size="2",
            weight="medium",
            color_scheme=rx.cond(danger, "red", "gray"),
        ),
        width="100%",
    )


def delete_user_dialog(u: UserRow) -> rx.Component:
    """Permanently remove a user, after showing exactly what is destroyed."""
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(
                rx.icon("trash-2", size=14),
                size="1",
                color_scheme="red",
                variant="soft",
                on_click=lambda: AdminState.open_delete_dialog(u.id),
            )
        ),
        rx.dialog.content(
            rx.dialog.title("Delete user"),
            rx.dialog.description(
                rx.hstack(
                    rx.text(AdminState.del_username, weight="bold"),
                    rx.text("·", color_scheme="gray"),
                    rx.text(AdminState.del_telegram_id, size="2",
                            color_scheme="gray"),
                    spacing="2",
                    align="center",
                ),
                size="2",
            ),
            rx.vstack(
                rx.cond(
                    AdminState.del_done,
                    rx.callout(
                        AdminState.del_message,
                        icon="circle-check",
                        color_scheme="green",
                        size="1",
                        width="100%",
                    ),
                    rx.fragment(
                        rx.vstack(
                            _impact_row("Orders", AdminState.del_orders,
                                        AdminState.del_orders > 0),
                            _impact_row("SMS orders", AdminState.del_sms),
                            _impact_row("Wallet top-ups", AdminState.del_topups),
                            _impact_row(
                                "Wallet balance",
                                f"${AdminState.del_balance}",
                                AdminState.del_balance != "0.00",
                            ),
                            spacing="2",
                            width="100%",
                        ),
                        rx.cond(
                            AdminState.del_is_deleted,
                            rx.callout(
                                "This account is deactivated. Nothing was "
                                "destroyed — restoring it lets the user sign "
                                "in and buy again.",
                                icon="info",
                                color_scheme="blue",
                                size="1",
                                width="100%",
                            ),
                            rx.cond(
                                AdminState.del_can_delete,
                                rx.callout(
                                    "Deactivate is reversible and keeps "
                                    "everything. Delete permanently destroys "
                                    "the user, their SMS orders, top-up "
                                    "sessions and wallet balance.",
                                    icon="triangle-alert",
                                    color_scheme="amber",
                                    size="1",
                                    width="100%",
                                ),
                                rx.callout(
                                    AdminState.del_reason
                                    + " Deactivate instead — it is reversible "
                                    "and keeps the order history.",
                                    icon="shield-alert",
                                    color_scheme="red",
                                    size="1",
                                    width="100%",
                                ),
                            ),
                        ),
                        rx.cond(
                            ~AdminState.del_is_deleted,
                            rx.vstack(
                                rx.text(
                                    "Reason (recorded in the activity log)",
                                    size="1",
                                    weight="medium",
                                ),
                                rx.input(
                                    placeholder="e.g. spam, abuse, rule violation",
                                    value=AdminState.del_reason_input,
                                    on_change=AdminState.set_del_reason,
                                    width="100%",
                                    size="2",
                                ),
                                spacing="1",
                                width="100%",
                            ),
                        ),
                        rx.cond(
                            AdminState.del_message != "",
                            rx.callout(
                                AdminState.del_message,
                                icon="triangle-alert",
                                color_scheme="red",
                                size="1",
                                width="100%",
                            ),
                        ),
                    ),
                ),
                rx.hstack(
                    rx.dialog.close(
                        rx.button(
                            rx.cond(AdminState.del_done, "Done", "Cancel"),
                            variant="soft",
                            color_scheme="gray",
                            on_click=AdminState.close_delete_dialog,
                            flex="1",
                        )
                    ),
                    rx.cond(
                        AdminState.del_done,
                        rx.fragment(),
                        rx.cond(
                            AdminState.del_is_deleted,
                            rx.button(
                                rx.icon("rotate-ccw", size=15),
                                "Restore account",
                                color_scheme="green",
                                on_click=AdminState.restore_user,
                                flex="1",
                            ),
                            rx.hstack(
                                rx.button(
                                    rx.icon("user-x", size=15),
                                    "Deactivate",
                                    color_scheme="amber",
                                    on_click=AdminState.soft_delete_user,
                                    flex="1",
                                ),
                                rx.button(
                                    rx.icon("trash-2", size=15),
                                    "Delete",
                                    color_scheme="red",
                                    variant="soft",
                                    disabled=~AdminState.del_can_delete,
                                    on_click=AdminState.confirm_delete_user,
                                    flex="1",
                                ),
                                spacing="2",
                                width="100%",
                            ),
                        ),
                    ),
                    spacing="2",
                    width="100%",
                ),
                spacing="3",
                width="100%",
                margin_top="1em",
            ),
            max_width="26em",
        ),
    )


def wallet_dialog(u: UserRow) -> rx.Component:
    """Credit/debit one user's wallet, scoped to that user.

    Uses Radix's own trigger rather than a state-controlled ``open`` prop —
    the dialog opens on click without a server round-trip, and the handler
    only loads that user's current balance into the form.
    """
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(
                rx.icon("wallet", size=14),
                "Adjust",
                size="1",
                variant="soft",
                on_click=lambda: AdminState.open_wallet_dialog(u.id),
            )
        ),
        rx.dialog.content(
            rx.dialog.title("Adjust wallet"),
            rx.dialog.description(
                rx.hstack(
                    rx.text(AdminState.wallet_username, weight="bold"),
                    rx.text("·", color_scheme="gray"),
                    rx.text(f"user #{AdminState.wallet_user_id}", size="2",
                            color_scheme="gray"),
                    spacing="2",
                    align="center",
                ),
                size="2",
            ),
            rx.vstack(
                rx.hstack(
                    rx.text("Current balance", size="2", color_scheme="gray"),
                    rx.spacer(),
                    rx.heading(f"${AdminState.wallet_balance}", size="5"),
                    width="100%",
                    align="center",
                ),
                rx.divider(),
                rx.vstack(
                    rx.text("Amount (USD)", size="1", weight="medium"),
                    rx.input(
                        placeholder="e.g. 5.00",
                        type="number",
                        value=AdminState.user_adjust_amount,
                        on_change=AdminState.set_user_adjust_amount,
                        width="100%",
                        size="3",
                        auto_focus=True,
                    ),
                    spacing="1",
                    width="100%",
                ),
                rx.cond(
                    AdminState.wallet_message != "",
                    rx.callout(
                        AdminState.wallet_message,
                        icon=rx.cond(
                            AdminState.wallet_ok,
                            "circle-check",
                            "triangle-alert",
                        ),
                        color_scheme=rx.cond(
                            AdminState.wallet_ok, "green", "red"
                        ),
                        size="1",
                        width="100%",
                    ),
                ),
                rx.hstack(
                    rx.button(
                        rx.icon("plus", size=15),
                        "Credit",
                        color_scheme="green",
                        on_click=AdminState.credit_user_wallet,
                        flex="1",
                    ),
                    rx.button(
                        rx.icon("minus", size=15),
                        "Debit",
                        color_scheme="orange",
                        on_click=AdminState.debit_user_wallet,
                        flex="1",
                    ),
                    spacing="2",
                    width="100%",
                ),
                rx.dialog.close(
                    rx.button(
                        "Done",
                        variant="soft",
                        color_scheme="gray",
                        width="100%",
                        on_click=AdminState.close_wallet_dialog,
                    )
                ),
                spacing="3",
                width="100%",
                margin_top="1em",
            ),
            max_width="24em",
        ),
    )


def users_tab() -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.hstack(
                card_header(
                    "users",
                    "Users",
                    "Toggle Active to suspend. Use Adjust to credit or "
                    "debit a user's wallet.",
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
            section_message(AdminState.users_message),
            rx.box(
                rx.table.root(
                    rx.table.header(
                        rx.table.row(
                            *[
                                sortable_header(
                                    label,
                                    col,
                                    AdminState.user_sort,
                                    AdminState.user_desc,
                                    AdminState.sort_users,
                                )
                                for label, col in [
                                    ("ID", "id"),
                                    ("Telegram ID", "telegram"),
                                    ("Username", "username"),
                                    ("Wallet", "balance"),
                                ]
                            ],
                            rx.table.column_header_cell("Status"),
                            rx.table.column_header_cell("Active"),
                            rx.table.column_header_cell("Block"),
                            rx.table.column_header_cell("Actions"),
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
# Overview tab — charts and at-a-glance health
# --------------------------------------------------------------------------- #
def overview_tab() -> rx.Component:
    return rx.vstack(
        rx.grid(
            chart_card(
                "Revenue by product",
                "Top earners, all time",
                rx.cond(
                    AdminState.revenue_by_product.length() > 0,
                    rx.recharts.bar_chart(
                        rx.recharts.bar(
                            data_key="revenue",
                            fill=rx.color("accent", 9),
                            radius=[4, 4, 0, 0],
                        ),
                        rx.recharts.x_axis(
                            data_key="name", tick_size=8, font_size="10px"
                        ),
                        rx.recharts.y_axis(font_size="10px"),
                        rx.recharts.graphing_tooltip(),
                        rx.recharts.cartesian_grid(
                            stroke_dasharray="3 3", vertical=False
                        ),
                        data=AdminState.revenue_by_product,
                        height=240,
                        width="100%",
                    ),
                    empty_chart("No revenue recorded yet"),
                ),
            ),
            chart_card(
                "Orders by status",
                "Distribution across all orders",
                rx.cond(
                    AdminState.orders_by_status.length() > 0,
                    rx.recharts.pie_chart(
                        rx.recharts.pie(
                            data=AdminState.orders_by_status,
                            data_key="value",
                            name_key="name",
                            inner_radius="55%",
                            outer_radius="80%",
                            padding_angle=2,
                        ),
                        rx.recharts.graphing_tooltip(),
                        rx.recharts.legend(font_size="11px"),
                        height=240,
                        width="100%",
                    ),
                    empty_chart("No orders yet"),
                ),
            ),
            columns=rx.breakpoints(initial="1", lg="2"),
            spacing="3",
            width="100%",
        ),
        chart_card(
            "Order volume",
            "Orders per day, last 14 days with activity",
            rx.cond(
                AdminState.orders_by_day.length() > 0,
                rx.recharts.area_chart(
                    rx.recharts.area(
                        data_key="orders",
                        stroke=rx.color("accent", 9),
                        fill=rx.color("accent", 5),
                        type_="monotone",
                    ),
                    rx.recharts.x_axis(data_key="day", font_size="10px"),
                    rx.recharts.y_axis(allow_decimals=False, font_size="10px"),
                    rx.recharts.graphing_tooltip(),
                    rx.recharts.cartesian_grid(
                        stroke_dasharray="3 3", vertical=False
                    ),
                    data=AdminState.orders_by_day,
                    height=220,
                    width="100%",
                ),
                empty_chart("No orders yet"),
            ),
        ),
        rx.card(
            rx.vstack(
                rx.hstack(
                    rx.icon(
                        "triangle-alert", size=17, color=rx.color("amber", 9)
                    ),
                    rx.heading("Low stock", size="3"),
                    rx.badge(
                        AdminState.stock_alert_count,
                        color_scheme=rx.cond(
                            AdminState.stock_alert_count > 0, "amber", "gray"
                        ),
                    ),
                    spacing="2",
                    align="center",
                ),
                rx.cond(
                    AdminState.stock_alert_count > 0,
                    rx.vstack(
                        rx.foreach(
                            AdminState.stock_alerts,
                            lambda p: rx.hstack(
                                rx.text(p.name, size="2"),
                                rx.spacer(),
                                rx.badge(
                                    f"{p.available} left",
                                    color_scheme=rx.cond(
                                        p.available == 0, "red", "amber"
                                    ),
                                ),
                                width="100%",
                                align="center",
                            ),
                        ),
                        spacing="2",
                        width="100%",
                    ),
                    rx.text(
                        "Every active product has healthy stock.",
                        size="1",
                        color_scheme="gray",
                    ),
                ),
                spacing="3",
                width="100%",
            ),
            size="2",
            width="100%",
        ),
        spacing="3",
        width="100%",
    )


# --------------------------------------------------------------------------- #
# Activity tab — admin audit log
# --------------------------------------------------------------------------- #
def _audit_row(e: AuditRow) -> rx.Component:
    return rx.table.row(
        rx.table.cell(rx.text(e.created_at, size="1", white_space="nowrap")),
        rx.table.cell(rx.badge(e.action, variant="soft")),
        rx.table.cell(rx.text(e.target, size="1")),
        rx.table.cell(rx.text(e.summary, size="1")),
        rx.table.cell(
            rx.cond(
                e.reason != "",
                rx.text(e.reason, size="1"),
                rx.text("—", size="1", color_scheme="gray"),
            )
        ),
        rx.table.cell(rx.text(e.actor, size="1", color_scheme="gray")),
        align="center",
    )


def activity_tab() -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.hstack(
                card_header(
                    "scroll-text",
                    "Activity log",
                    "Every privileged action — deletions, wallet changes and "
                    "password updates — with who, when and why.",
                ),
                rx.spacer(),
                search_box(
                    "Search activity…",
                    AdminState.audit_search,
                    AdminState.set_audit_search,
                ),
                width="100%",
                align="start",
                wrap="wrap",
            ),
            rx.hstack(
                result_count(AdminState.audit_result_count, "entries"),
                rx.spacer(),
                width="100%",
            ),
            rx.cond(
                AdminState.audit_result_count > 0,
                rx.box(
                    rx.table.root(
                        rx.table.header(
                            rx.table.row(
                                rx.table.column_header_cell("When (UTC)"),
                                rx.table.column_header_cell("Action"),
                                rx.table.column_header_cell("Target"),
                                rx.table.column_header_cell("Details"),
                                rx.table.column_header_cell("Reason"),
                                rx.table.column_header_cell("By"),
                            )
                        ),
                        rx.table.body(
                            rx.foreach(AdminState.filtered_audit, _audit_row)
                        ),
                        variant="surface",
                        size="1",
                        width="100%",
                    ),
                    overflow_x="auto",
                    width="100%",
                ),
                rx.center(
                    rx.vstack(
                        rx.icon("scroll-text", size=22, opacity=0.4),
                        rx.text(
                            "No activity recorded yet.",
                            size="1",
                            color_scheme="gray",
                        ),
                        spacing="2",
                        align="center",
                    ),
                    height="10em",
                    width="100%",
                ),
            ),
            spacing="4",
            width="100%",
        ),
        size="3",
        width="100%",
    )


# --------------------------------------------------------------------------- #
# Settings tab — account security
# --------------------------------------------------------------------------- #
def _strength_meter() -> rx.Component:
    return rx.cond(
        AdminState.new_password != "",
        rx.vstack(
            rx.progress(
                value=AdminState.password_score * 25,
                color_scheme=rx.cond(
                    AdminState.password_score >= 4,
                    "green",
                    rx.cond(AdminState.password_score >= 2, "amber", "red"),
                ),
                size="1",
                width="100%",
            ),
            rx.text(
                AdminState.password_strength_label,
                size="1",
                color_scheme="gray",
            ),
            spacing="1",
            width="100%",
        ),
    )


def settings_tab() -> rx.Component:
    return rx.vstack(
        rx.cond(
            AdminState.using_env_password,
            rx.callout(
                "You are signed in with the ADMIN_PASSWORD from .env. Set a "
                "password here — it is stored hashed in the database and the "
                ".env value stops working.",
                icon="triangle-alert",
                color_scheme="amber",
                size="1",
                width="100%",
            ),
        ),
        rx.card(
            rx.vstack(
                card_header(
                    "key-round",
                    "Change admin password",
                    "Stored as a salted PBKDF2-SHA256 hash. Changing it "
                    "immediately revokes the old password.",
                ),
                rx.vstack(
                    rx.text("Current password", size="1", weight="medium"),
                    rx.input(
                        type="password",
                        placeholder="Current password",
                        value=AdminState.current_password,
                        on_change=AdminState.set_current_password,
                        width="100%",
                        size="2",
                    ),
                    spacing="1",
                    width="100%",
                ),
                rx.vstack(
                    rx.text("New password", size="1", weight="medium"),
                    rx.input(
                        type="password",
                        placeholder=(
                            f"At least {admin_auth.MIN_PASSWORD_LENGTH} "
                            "characters"
                        ),
                        value=AdminState.new_password,
                        on_change=AdminState.set_new_password,
                        width="100%",
                        size="2",
                    ),
                    _strength_meter(),
                    spacing="1",
                    width="100%",
                ),
                rx.vstack(
                    rx.text("Confirm new password", size="1", weight="medium"),
                    rx.input(
                        type="password",
                        placeholder="Repeat the new password",
                        value=AdminState.confirm_password,
                        on_change=AdminState.set_confirm_password,
                        width="100%",
                        size="2",
                    ),
                    spacing="1",
                    width="100%",
                ),
                rx.button(
                    rx.icon("shield-check", size=16),
                    "Update password",
                    on_click=AdminState.change_password,
                    size="2",
                ),
                rx.cond(
                    AdminState.password_message != "",
                    rx.callout(
                        AdminState.password_message,
                        icon=rx.cond(
                            AdminState.password_ok,
                            "circle-check",
                            "triangle-alert",
                        ),
                        color_scheme=rx.cond(
                            AdminState.password_ok, "green", "red"
                        ),
                        size="1",
                        width="100%",
                    ),
                ),
                spacing="3",
                width="100%",
            ),
            size="3",
            width="100%",
            max_width="34em",
        ),
        rx.card(
            rx.vstack(
                card_header(
                    "clock",
                    "Session",
                    "Automatic sign-out protects the panel on a shared or "
                    "unattended machine.",
                ),
                rx.hstack(
                    rx.text("Idle timeout", size="2"),
                    rx.spacer(),
                    rx.badge(
                        f"{SESSION_IDLE_SECONDS // 60} minutes",
                        color_scheme="gray",
                    ),
                    width="100%",
                ),
                rx.hstack(
                    rx.text("Signs out in", size="2"),
                    rx.spacer(),
                    rx.badge(
                        f"{AdminState.idle_seconds_left}s", color_scheme="blue"
                    ),
                    width="100%",
                ),
                spacing="3",
                width="100%",
            ),
            size="3",
            width="100%",
            max_width="34em",
        ),
        spacing="3",
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
                rx.box(
                    rx.icon("store", size=18, color="white"),
                    background=(
                        f"linear-gradient(135deg, {rx.color('accent', 9)}, "
                        f"{rx.color('accent', 11)})"
                    ),
                    border_radius="9px",
                    padding="0.4em",
                    display="flex",
                ),
                rx.vstack(
                    rx.heading("Bondom Admin", size="4", line_height="1.1"),
                    rx.text(
                        "Control panel",
                        size="1",
                        color_scheme="gray",
                        display=rx.breakpoints(initial="none", sm="block"),
                    ),
                    spacing="0",
                    align="start",
                ),
                spacing="3",
                align="center",
            ),
            rx.spacer(),
            rx.tooltip(
                rx.badge(
                    rx.icon("clock", size=12),
                    f"{AdminState.idle_seconds_left}s",
                    color_scheme=rx.cond(
                        AdminState.idle_seconds_left < 120, "amber", "gray"
                    ),
                    variant="soft",
                ),
                content="Time left before automatic sign-out",
            ),
            rx.button(
                rx.icon("refresh-cw", size=15),
                rx.text(
                    "Refresh",
                    display=rx.breakpoints(initial="none", sm="block"),
                ),
                on_click=[AdminState.touch, AdminState.load_all],
                variant="soft",
                size="2",
            ),
            rx.button(
                rx.icon("log-out", size=15),
                rx.text(
                    "Sign out",
                    display=rx.breakpoints(initial="none", sm="block"),
                ),
                on_click=lambda: AdminState.logout(""),
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
        z_index="20",
        backdrop_filter="blur(12px) saturate(180%)",
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
                    rx.box(
                        rx.tabs.list(
                            _tab_trigger("layout-dashboard", "Overview", "overview"),
                            _tab_trigger("boxes", "Products", "products"),
                            _tab_trigger("shopping-cart", "Orders", "orders"),
                            _tab_trigger("users", "Users", "users"),
                            _tab_trigger("smartphone", "SMS", "sms"),
                            _tab_trigger("megaphone", "Marketing", "marketing"),
                            _tab_trigger("scroll-text", "Activity", "activity"),
                            _tab_trigger("settings", "Settings", "settings"),
                            size="2",
                        ),
                        # Tab strip scrolls rather than wrapping on phones.
                        overflow_x="auto",
                        width="100%",
                    ),
                    rx.tabs.content(
                        overview_tab(), value="overview", padding_top="1.2em"
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
                    rx.tabs.content(
                        activity_tab(), value="activity", padding_top="1.2em"
                    ),
                    rx.tabs.content(
                        settings_tab(), value="settings", padding_top="1.2em"
                    ),
                    default_value="overview",
                    width="100%",
                ),
                spacing="4",
                width="100%",
                padding=rx.breakpoints(initial="0.8em", sm="1.2em"),
                max_width="78rem",
                margin_x="auto",
            ),
            width="100%",
        ),
        # Any interaction anywhere in the panel resets the idle timer.
        # VStack exposes pointer/scroll triggers but not key events, so
        # mutating handlers also call touch() directly.
        on_mouse_down=AdminState.touch,
        on_scroll=AdminState.touch,
        spacing="0",
        width="100%",
    )


def login_view() -> rx.Component:
    return rx.center(
        rx.card(
            rx.vstack(
                rx.box(
                    rx.icon("store", size=24, color="white"),
                    background=(
                        f"linear-gradient(135deg, {rx.color('accent', 9)}, "
                        f"{rx.color('accent', 11)})"
                    ),
                    border_radius="14px",
                    padding="0.7em",
                    display="flex",
                ),
                rx.vstack(
                    rx.heading("Bondom Account", size="6"),
                    rx.text("Admin sign in", size="2", color_scheme="gray"),
                    spacing="1",
                    align="center",
                ),
                rx.cond(
                    AdminState.session_notice != "",
                    rx.callout(
                        AdminState.session_notice,
                        icon="clock",
                        color_scheme="amber",
                        size="1",
                        width="100%",
                    ),
                ),
                rx.form(
                    rx.vstack(
                        rx.input(
                            placeholder="Admin password",
                            type="password",
                            value=AdminState.password_input,
                            on_change=AdminState.set_password_input,
                            width="100%",
                            size="3",
                            auto_focus=True,
                        ),
                        rx.button(
                            rx.icon("lock-open", size=16),
                            "Sign in",
                            type="submit",
                            width="100%",
                            size="3",
                        ),
                        spacing="3",
                        width="100%",
                    ),
                    on_submit=lambda _form: AdminState.login,
                    width="100%",
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
        padding="1em",
    )


@rx.page(route="/", title="Bondom Account — Admin", on_load=AdminState.load_all)
def index() -> rx.Component:
    return rx.cond(AdminState.authed, dashboard_view(), login_view())


app = rx.App(
    theme=rx.theme(
        accent_color="indigo",
        gray_color="slate",
        radius="large",
        scaling="100%",
    )
)
