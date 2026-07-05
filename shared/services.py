"""Business logic for the digital product store.

Shared by the FastAPI backend, the aiogram bot and the Reflex admin
panel. The service layer owns transaction boundaries: every function
runs inside ``transaction_scope(session)`` — its own atomic transaction
when called standalone, or the caller's if one is already open.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models import (
    AppSetting,
    Inventory,
    InventoryStatus,
    Order,
    OrderStatus,
    Payment,
    Product,
    User,
)
from shared.schemas import OrderCreate, ProductCreate


@asynccontextmanager
async def transaction_scope(session: AsyncSession) -> AsyncIterator[None]:
    """Begin a transaction, or join the caller's if one is already open.

    Makes every service composable: called standalone it commits (or rolls
    back) its own unit of work; called inside a caller-managed
    ``session.begin()`` it becomes part of that larger atomic unit.
    """
    if session.in_transaction():
        yield
    else:
        async with session.begin():
            yield


# --------------------------------------------------------------------------- #
# Domain exceptions
# --------------------------------------------------------------------------- #
class ServiceError(Exception):
    """Base class for domain-level errors raised by the service layer."""


class ProductNotFoundError(ServiceError):
    def __init__(self, product_id: int) -> None:
        self.product_id = product_id
        super().__init__(f"Product {product_id} not found or inactive")


class UserNotFoundError(ServiceError):
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__(f"User {user_id} not found")


class UserInactiveError(ServiceError):
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__(f"User {user_id} is suspended and cannot purchase")


class OrderNotFoundError(ServiceError):
    def __init__(self, order_id: int) -> None:
        self.order_id = order_id
        super().__init__(f"Order {order_id} not found")


class OutOfStockError(ServiceError):
    def __init__(self, product_id: int, requested: int, available: int) -> None:
        self.product_id = product_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"Product {product_id}: requested {requested} item(s), "
            f"only {available} in stock"
        )


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
async def get_or_create_user(
    session: AsyncSession, telegram_id: int, username: str | None = None
) -> User:
    """Upsert a user by Telegram id (called on every bot interaction)."""
    async with transaction_scope(session):
        user = await session.scalar(
            select(User).where(User.telegram_id == telegram_id)
        )
        if user is None:
            user = User(telegram_id=telegram_id, username=username)
            session.add(user)
        elif username and user.username != username:
            user.username = username
    return user


async def toggle_user_status(
    session: AsyncSession, user_id: int, is_active: bool
) -> User:
    """Suspend or reactivate a user (control interface for the admin panel).

    The bot checks ``User.is_active`` inside
    :func:`create_order_and_allocate_stock` before any purchase, so a
    toggle here takes effect on the very next order attempt.
    """
    async with transaction_scope(session):
        user = await session.get(User, user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        user.is_active = is_active
    return user


async def list_users(session: AsyncSession, limit: int = 200) -> list[User]:
    # Read-only services also scope an explicit transaction so the session
    # is left clean (no dangling autobegin) for the caller's next
    # `session.begin()` on the same session.
    async with transaction_scope(session):
        result = await session.scalars(
            select(User).order_by(User.created_at.desc()).limit(limit)
        )
        return list(result.all())


async def list_active_telegram_ids(session: AsyncSession) -> list[int]:
    """Return Telegram IDs for active users who can receive announcements."""
    async with transaction_scope(session):
        result = await session.scalars(
            select(User.telegram_id).where(User.is_active.is_(True))
        )
        return [int(x) for x in result.all()]


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
async def create_product(session: AsyncSession, payload: ProductCreate) -> Product:
    """Persist a new product and return it."""
    product = Product(**payload.model_dump())
    async with transaction_scope(session):
        session.add(product)
    return product


async def list_products(
    session: AsyncSession, only_active: bool = False
) -> list[Product]:
    stmt = select(Product).order_by(Product.id)
    if only_active:
        stmt = stmt.where(Product.is_active.is_(True))
    async with transaction_scope(session):
        result = await session.scalars(stmt)
        return list(result.all())


class ProductOverview(NamedTuple):
    product: Product
    available: int  # stock left to sell
    sold: int  # units already allocated to orders
    revenue: Decimal  # price * sold


class InventoryImportResult(NamedTuple):
    inserted: int
    skipped_empty: int
    skipped_duplicate: int


async def list_products_with_stock(
    session: AsyncSession,
) -> list[tuple[Product, int]]:
    """Products joined with their count of available inventory items."""
    available = func.count(Inventory.id).filter(
        Inventory.status == InventoryStatus.AVAILABLE
    )
    stmt = (
        select(Product, available)
        .outerjoin(Inventory, Inventory.product_id == Product.id)
        .group_by(Product.id)
        .order_by(Product.id)
    )
    async with transaction_scope(session):
        result = await session.execute(stmt)
        return [(product, int(stock)) for product, stock in result.all()]


async def list_product_overviews(
    session: AsyncSession,
) -> list[ProductOverview]:
    """Per-product admin view: stock left, units sold, and revenue.

    ``available`` = items a buyer can still purchase now; ``sold`` = items
    already allocated to an order (stock consumed after a buy).
    """
    available = func.count(Inventory.id).filter(
        Inventory.status == InventoryStatus.AVAILABLE
    )
    sold = func.count(Inventory.id).filter(
        Inventory.status == InventoryStatus.SOLD
    )
    stmt = (
        select(Product, available, sold)
        .outerjoin(Inventory, Inventory.product_id == Product.id)
        .group_by(Product.id)
        .order_by(Product.id)
    )
    async with transaction_scope(session):
        rows = (await session.execute(stmt)).all()
    return [
        ProductOverview(
            product=product,
            available=int(avail),
            sold=int(nsold),
            revenue=product.price * nsold,
        )
        for product, avail, nsold in rows
    ]


async def update_product_price(
    session: AsyncSession, product_id: int, price: Decimal
) -> Product:
    async with transaction_scope(session):
        product = await session.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        product.price = price
    return product


async def update_product_warranty_days(
    session: AsyncSession, product_id: int, warranty_days: int
) -> Product:
    """Set product warranty days (0 disables warranty text on delivery)."""
    if warranty_days < 0:
        raise ServiceError("warranty_days must be >= 0")
    async with transaction_scope(session):
        product = await session.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        product.warranty_days = warranty_days
    return product


async def set_product_active(
    session: AsyncSession, product_id: int, is_active: bool
) -> Product:
    """Show/hide a product in the bot without deleting it or its stock."""
    async with transaction_scope(session):
        product = await session.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        product.is_active = is_active
    return product


async def rename_product(
    session: AsyncSession, product_id: int, new_name: str
) -> Product:
    """Rename a product displayed in bot/admin."""
    clean = new_name.strip()
    if not clean:
        raise ServiceError("Product name cannot be empty")
    async with transaction_scope(session):
        product = await session.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        product.name = clean
    return product


async def delete_product(session: AsyncSession, product_id: int) -> None:
    """Delete a product and all its inventory immediately."""
    async with transaction_scope(session):
        product = await session.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError(product_id)

        rows = list(
            (
                await session.scalars(
                    select(Inventory).where(Inventory.product_id == product_id)
                )
            ).all()
        )
        for row in rows:
            await session.delete(row)
        await session.delete(product)


BOT_SHOW_STOCK_KEY = "bot_show_stock"
PRODUCT_NOTE_KEY_PREFIX = "product_note:"


async def get_bot_show_stock(session: AsyncSession) -> bool:
    """Whether bot should display exact stock counts in product list."""
    async with transaction_scope(session):
        setting = await session.get(AppSetting, BOT_SHOW_STOCK_KEY)
        if setting is None:
            return True
        return setting.value.lower() in {"1", "true", "yes", "on"}


async def set_bot_show_stock(session: AsyncSession, enabled: bool) -> bool:
    """Persist bot stock visibility toggle."""
    async with transaction_scope(session):
        setting = await session.get(AppSetting, BOT_SHOW_STOCK_KEY)
        value = "true" if enabled else "false"
        if setting is None:
            session.add(AppSetting(key=BOT_SHOW_STOCK_KEY, value=value))
        else:
            setting.value = value
    return enabled


def _product_note_key(product_id: int) -> str:
    return f"{PRODUCT_NOTE_KEY_PREFIX}{product_id}"


async def get_product_client_note(
    session: AsyncSession, product_id: int
) -> str | None:
    """Get optional client note shown on delivery for a product."""
    async with transaction_scope(session):
        setting = await session.get(AppSetting, _product_note_key(product_id))
        if setting is None:
            return None
        value = setting.value.strip()
        return value or None


async def set_product_client_note(
    session: AsyncSession, product_id: int, note: str
) -> str | None:
    """Set optional delivery note for a product; blank removes it."""
    key = _product_note_key(product_id)
    value = note.strip()
    async with transaction_scope(session):
        setting = await session.get(AppSetting, key)
        if not value:
            if setting is not None:
                await session.delete(setting)
            return None
        if setting is None:
            session.add(AppSetting(key=key, value=value))
        else:
            setting.value = value
    return value


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
async def bulk_add_inventory(
    session: AsyncSession, product_id: int, items: list[str]
) -> int:
    """Insert one inventory row per non-empty line. Returns rows created."""
    result = await bulk_add_inventory_with_report(session, product_id, items)
    return result.inserted


async def bulk_add_inventory_with_report(
    session: AsyncSession, product_id: int, items: list[str]
) -> InventoryImportResult:
    """Insert inventory lines with detailed counters.

    Rule: one non-empty line = one account.
    """
    raw = [line.replace("\ufeff", "").strip() for line in items]
    skipped_empty = sum(1 for line in raw if not line)

    cleaned = [line for line in raw if line]
    if not cleaned:
        return InventoryImportResult(
            inserted=0,
            skipped_empty=skipped_empty,
            skipped_duplicate=0,
        )

    # Keep only unique values from this upload batch.
    unique = list(dict.fromkeys(cleaned))
    skipped_duplicate = len(cleaned) - len(unique)

    async with transaction_scope(session):
        product = await session.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError(product_id)

        existing = set(
            (
                await session.scalars(
                    select(Inventory.data).where(Inventory.product_id == product_id)
                )
            ).all()
        )
        to_insert = [line for line in unique if line not in existing]
        skipped_duplicate += len(unique) - len(to_insert)
        if not to_insert:
            return InventoryImportResult(
                inserted=0,
                skipped_empty=skipped_empty,
                skipped_duplicate=skipped_duplicate,
            )

        session.add_all(
            Inventory(product_id=product_id, data=line) for line in to_insert
        )
    return InventoryImportResult(
        inserted=len(to_insert),
        skipped_empty=skipped_empty,
        skipped_duplicate=skipped_duplicate,
    )


async def delete_available_inventory(
    session: AsyncSession, product_id: int
) -> int:
    """Delete unsold stock for a product (e.g. remove demo/test items).

    Only ``available`` rows are removed — sold items stay for order
    history. Returns the number of rows deleted.
    """
    async with transaction_scope(session):
        rows = list(
            (
                await session.scalars(
                    select(Inventory).where(
                        Inventory.product_id == product_id,
                        Inventory.status == InventoryStatus.AVAILABLE,
                    )
                )
            ).all()
        )
        for row in rows:
            await session.delete(row)
    return len(rows)


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
async def create_order_and_allocate_stock(
    session: AsyncSession, payload: OrderCreate
) -> Order:
    """Atomically create an order and allocate inventory to it.

    Inside a single transaction:
      1. Validate the user (must exist AND be active) and the product.
      2. Lock the oldest ``available`` inventory rows for the product using
         ``FOR UPDATE SKIP LOCKED`` — concurrent orders skip rows already
         locked by another transaction instead of blocking on them, so the
         same item can never be sold twice and throughput stays high.
      3. Create the order, mark the rows ``sold`` and link them to it.

    Any failure (including :class:`OutOfStockError`) rolls the entire
    transaction back — no half-created orders, no orphaned allocations.
    """
    async with transaction_scope(session):
        user = await session.get(User, payload.user_id)
        if user is None:
            raise UserNotFoundError(payload.user_id)
        if not user.is_active:
            raise UserInactiveError(payload.user_id)

        product = await session.get(Product, payload.product_id)
        if product is None or not product.is_active:
            raise ProductNotFoundError(payload.product_id)

        stmt = (
            select(Inventory)
            .where(
                Inventory.product_id == payload.product_id,
                Inventory.status == InventoryStatus.AVAILABLE,
            )
            .order_by(Inventory.created_at, Inventory.id)
            .limit(payload.quantity)
            .with_for_update(skip_locked=True)
        )
        allocated = list((await session.scalars(stmt)).all())

        if len(allocated) < payload.quantity:
            raise OutOfStockError(
                product_id=payload.product_id,
                requested=payload.quantity,
                available=len(allocated),
            )

        order = Order(
            user_id=payload.user_id,
            total_price=product.price * Decimal(payload.quantity),
            status=OrderStatus.PENDING,
        )
        # Keep items attached for immediate serialization by callers.
        order.items = allocated
        session.add(order)
        await session.flush()  # ensure order.id exists before linking inventory

        for item in allocated:
            item.status = InventoryStatus.SOLD
            item.assigned_order_id = order.id

    return order


async def get_order_with_items(session: AsyncSession, order_id: int) -> Order:
    async with transaction_scope(session):
        order = await session.scalar(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
        )
    if order is None:
        raise OrderNotFoundError(order_id)
    return order


async def mark_order_paid(session: AsyncSession, order_id: int) -> Order:
    async with transaction_scope(session):
        order = await session.scalar(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
        )
        if order is None:
            raise OrderNotFoundError(order_id)
        order.status = OrderStatus.PAID
    return order


async def mark_order_delivered(session: AsyncSession, order_id: int) -> Order:
    async with transaction_scope(session):
        order = await session.get(Order, order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        order.status = OrderStatus.DELIVERED
    return order


async def cancel_order_and_release_inventory(
    session: AsyncSession, order_id: int
) -> Order:
    """Cancel a pending order and put reserved items back into stock."""
    async with transaction_scope(session):
        order = await session.get(Order, order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        if order.status is not OrderStatus.PENDING:
            return order

        rows = list(
            (
                await session.scalars(
                    select(Inventory).where(Inventory.assigned_order_id == order_id)
                )
            ).all()
        )
        for item in rows:
            item.status = InventoryStatus.AVAILABLE
            item.assigned_order_id = None
        order.status = OrderStatus.CANCELED
        await session.flush()
    return order


async def list_orders(session: AsyncSession, limit: int = 200) -> list[Order]:
    """Recent orders with user preloaded (for the admin panel table)."""
    async with transaction_scope(session):
        result = await session.scalars(
            select(Order)
            .options(selectinload(Order.user))
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
        )
        return list(result.all())


async def clear_all_orders_for_fresh_revenue(session: AsyncSession) -> int:
    """Delete all orders so revenue starts fresh from now.

    Behavior:
      - Pending orders: reserved inventory is released back to AVAILABLE.
      - Paid/Delivered orders: inventory remains SOLD (not resellable).
      - Payments are removed automatically via FK cascade.

    Returns the number of deleted orders.
    """
    async with transaction_scope(session):
        orders = list((await session.scalars(select(Order))).all())
        order_ids = [o.id for o in orders]

        for order in orders:
            rows = list(
                (
                    await session.scalars(
                        select(Inventory).where(
                            Inventory.assigned_order_id == order.id
                        )
                    )
                ).all()
            )
            if order.status is OrderStatus.PENDING:
                for item in rows:
                    item.status = InventoryStatus.AVAILABLE
                    item.assigned_order_id = None
            else:
                for item in rows:
                    item.assigned_order_id = None

        if order_ids:
            payments = list(
                (
                    await session.scalars(
                        select(Payment).where(Payment.order_id.in_(order_ids))
                    )
                ).all()
            )
            for payment in payments:
                await session.delete(payment)

        for order in orders:
            await session.delete(order)

        await session.flush()
        return len(orders)


class StoreStats(NamedTuple):
    total_users: int
    total_orders: int
    paid_orders: int  # paid or delivered
    revenue: Decimal  # sum of paid/delivered order totals
    available_stock: int  # items ready to sell across all products


async def get_store_stats(session: AsyncSession) -> StoreStats:
    """Headline KPIs for the admin dashboard."""
    paid_states = (OrderStatus.PAID, OrderStatus.DELIVERED)
    async with transaction_scope(session):
        total_users = await session.scalar(
            select(func.count(User.id))
        )
        total_orders = await session.scalar(select(func.count(Order.id)))
        paid_orders = await session.scalar(
            select(func.count(Order.id)).where(Order.status.in_(paid_states))
        )
        revenue = await session.scalar(
            select(func.coalesce(func.sum(Order.total_price), 0)).where(
                Order.status.in_(paid_states)
            )
        )
        available_stock = await session.scalar(
            select(func.count(Inventory.id)).where(
                Inventory.status == InventoryStatus.AVAILABLE
            )
        )
    return StoreStats(
        total_users=int(total_users or 0),
        total_orders=int(total_orders or 0),
        paid_orders=int(paid_orders or 0),
        revenue=Decimal(revenue or 0),
        available_stock=int(available_stock or 0),
    )
