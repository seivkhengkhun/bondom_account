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
    AgencyEarning,
    AgencyEarningStatus,
    AppSetting,
    Inventory,
    InventoryStatus,
    Order,
    OrderStatus,
    Payment,
    Payout,
    PayoutStatus,
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


class UserPermanentlyBlockedError(ServiceError):
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__(
            f"User {user_id} is permanently blocked and cannot use the service"
        )


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


class InsufficientBalanceError(ServiceError):
    def __init__(self, user_id: int, required: Decimal, balance: Decimal) -> None:
        self.user_id = user_id
        self.required = required
        self.balance = balance
        super().__init__(
            f"User {user_id} balance {balance:.2f} is less than required {required:.2f}"
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


async def get_user_by_telegram_id(
    session: AsyncSession, telegram_id: int
) -> User | None:
    """Return user by Telegram id, or None when unknown."""
    async with transaction_scope(session):
        return await session.scalar(select(User).where(User.telegram_id == telegram_id))


USER_BLOCK_KEY_PREFIX = "user_blocked:"


def _user_block_key(user_id: int) -> str:
    return f"{USER_BLOCK_KEY_PREFIX}{user_id}"


async def is_user_blocked(session: AsyncSession, user_id: int) -> bool:
    """Return True if user has been permanently blocked by admin."""
    async with transaction_scope(session):
        row = await session.get(AppSetting, _user_block_key(user_id))
        return row is not None


async def list_blocked_user_ids(session: AsyncSession) -> set[int]:
    """Return all permanently blocked user ids."""
    async with transaction_scope(session):
        rows = list(
            (
                await session.scalars(
                    select(AppSetting.key).where(
                        AppSetting.key.like(f"{USER_BLOCK_KEY_PREFIX}%")
                    )
                )
            ).all()
        )
    blocked: set[int] = set()
    for key in rows:
        suffix = key.removeprefix(USER_BLOCK_KEY_PREFIX)
        if suffix.isdigit():
            blocked.add(int(suffix))
    return blocked


async def block_user_forever(session: AsyncSession, user_id: int) -> User:
    """Permanently block a user and disable future access."""
    async with transaction_scope(session):
        user = await session.get(User, user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        user.is_active = False
        key = _user_block_key(user_id)
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value="true"))
    return user


async def unblock_user(session: AsyncSession, user_id: int) -> User:
    """Remove permanent block marker and reactivate user access."""
    async with transaction_scope(session):
        user = await session.get(User, user_id)
        if user is None:
            raise UserNotFoundError(user_id)

        key = _user_block_key(user_id)
        row = await session.get(AppSetting, key)
        if row is not None:
            await session.delete(row)

        user.is_active = True
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
        if is_active and await is_user_blocked(session, user_id):
            raise UserPermanentlyBlockedError(user_id)
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
        blocked = await list_blocked_user_ids(session)
        users = list(
            (
                await session.scalars(
                    select(User).where(User.is_active.is_(True))
                )
            ).all()
        )
        return [int(u.telegram_id) for u in users if u.id not in blocked]


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
async def create_product(
    session: AsyncSession,
    payload: ProductCreate,
    owner_id: int | None = None,
) -> Product:
    """Persist a new product and return it.

    ``owner_id`` set = an agency's product (marketplace); None = house
    product owned by the platform.
    """
    product = Product(**payload.model_dump())
    if owner_id is not None:
        product.owner_id = owner_id
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


async def update_product_category(
    session: AsyncSession, product_id: int, category: str
) -> Product:
    """Move a product to a category (a new name creates the category)."""
    value = category.strip()
    if not value:
        raise ServiceError("category must not be empty")
    async with transaction_scope(session):
        product = await session.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        product.category = value
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
USER_BALANCE_KEY_PREFIX = "user_balance:"
COMMISSION_RATE_KEY = "commission_rate"
AGENCY_BALANCE_KEY_PREFIX = "agency_earnings:"
DEFAULT_COMMISSION_RATE = Decimal("0.05")


def _user_balance_key(user_id: int) -> str:
    return f"{USER_BALANCE_KEY_PREFIX}{user_id}"


def _agency_balance_key(user_id: int) -> str:
    return f"{AGENCY_BALANCE_KEY_PREFIX}{user_id}"


def _to_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


async def get_user_balance(session: AsyncSession, user_id: int) -> Decimal:
    """Get wallet balance for a user (USD)."""
    async with transaction_scope(session):
        row = await session.get(AppSetting, _user_balance_key(user_id))
        if row is None:
            return Decimal("0.00")
        try:
            return _to_money(Decimal(row.value))
        except Exception:
            return Decimal("0.00")


async def add_user_balance(
    session: AsyncSession, user_id: int, amount: Decimal
) -> Decimal:
    """Increase user wallet balance and return updated amount."""
    if amount <= 0:
        raise ServiceError("Top-up amount must be greater than 0")
    async with transaction_scope(session):
        key = _user_balance_key(user_id)
        row = await session.get(AppSetting, key)
        current = Decimal("0.00") if row is None else Decimal(row.value)
        updated = _to_money(current + amount)
        if row is None:
            session.add(AppSetting(key=key, value=str(updated)))
        else:
            row.value = str(updated)
        return updated


async def spend_user_balance(
    session: AsyncSession, user_id: int, amount: Decimal
) -> Decimal:
    """Decrease wallet balance when sufficient and return updated amount."""
    if amount <= 0:
        raise ServiceError("Debit amount must be greater than 0")
    async with transaction_scope(session):
        key = _user_balance_key(user_id)
        row = await session.get(AppSetting, key)
        current = Decimal("0.00") if row is None else Decimal(row.value)
        if current < amount:
            raise InsufficientBalanceError(user_id, amount, _to_money(current))
        updated = _to_money(current - amount)
        if row is None:
            session.add(AppSetting(key=key, value=str(updated)))
        else:
            row.value = str(updated)
        return updated


async def adjust_user_balance(
    session: AsyncSession, user_id: int, delta: Decimal
) -> Decimal:
    """Adjust wallet by signed delta (admin tool); returns updated balance."""
    if delta == 0:
        raise ServiceError("Adjustment amount cannot be zero")

    async with transaction_scope(session):
        user = await session.get(User, user_id)
        if user is None:
            raise UserNotFoundError(user_id)

    if delta > 0:
        return await add_user_balance(session, user_id, delta)
    return await spend_user_balance(session, user_id, abs(delta))


# --------------------------------------------------------------------------- #
# Marketplace: commission rate, agency earnings balance, per-order split
# --------------------------------------------------------------------------- #
async def get_commission_rate(session: AsyncSession) -> Decimal:
    """Platform commission fraction (e.g. 0.05 = 5%). DB overrides default."""
    async with transaction_scope(session):
        row = await session.get(AppSetting, COMMISSION_RATE_KEY)
    if row is None:
        return DEFAULT_COMMISSION_RATE
    try:
        rate = Decimal(row.value)
    except Exception:
        return DEFAULT_COMMISSION_RATE
    return rate if Decimal("0") <= rate < Decimal("1") else DEFAULT_COMMISSION_RATE


async def set_commission_rate(session: AsyncSession, rate: Decimal) -> Decimal:
    """Persist the platform commission fraction (0 ≤ rate < 1)."""
    if not (Decimal("0") <= rate < Decimal("1")):
        raise ServiceError("Commission rate must be between 0 and 1 (e.g. 0.05)")
    value = rate.quantize(Decimal("0.0001"))
    async with transaction_scope(session):
        row = await session.get(AppSetting, COMMISSION_RATE_KEY)
        if row is None:
            session.add(AppSetting(key=COMMISSION_RATE_KEY, value=str(value)))
        else:
            row.value = str(value)
    return value


async def get_agency_balance(session: AsyncSession, user_id: int) -> Decimal:
    """Withdrawable earnings balance for an agency (USD)."""
    async with transaction_scope(session):
        row = await session.get(AppSetting, _agency_balance_key(user_id))
        if row is None:
            return Decimal("0.00")
        try:
            return _to_money(Decimal(row.value))
        except Exception:
            return Decimal("0.00")


async def _add_agency_balance(
    session: AsyncSession, user_id: int, amount: Decimal
) -> Decimal:
    async with transaction_scope(session):
        key = _agency_balance_key(user_id)
        row = await session.get(AppSetting, key)
        current = Decimal("0.00") if row is None else Decimal(row.value)
        updated = _to_money(current + amount)
        if row is None:
            session.add(AppSetting(key=key, value=str(updated)))
        else:
            row.value = str(updated)
        return updated


async def _order_seller_id(session: AsyncSession, order: Order) -> int | None:
    """The agency that owns the order's product, or None for house products."""
    async with transaction_scope(session):
        item = await session.scalar(
            select(Inventory).where(Inventory.assigned_order_id == order.id).limit(1)
        )
        if item is None:
            return None
        product = await session.get(Product, item.product_id)
    if product is None or product.owner_id is None:
        return None
    return product.owner_id


async def record_order_earning(
    session: AsyncSession, order_id: int
) -> AgencyEarning | None:
    """Record the commission split for a delivered agency order.

    House products (no owner) record nothing — the platform keeps 100%.
    Idempotent: one earning row per order; a second call is a no-op.
    Credits the agency's withdrawable earnings balance with the net.
    """
    async with transaction_scope(session):
        existing = await session.scalar(
            select(AgencyEarning).where(AgencyEarning.order_id == order_id)
        )
        if existing is not None:
            return existing
        order = await session.get(Order, order_id)
        if order is None:
            return None

    seller_id = await _order_seller_id(session, order)
    if seller_id is None:
        return None

    rate = await get_commission_rate(session)
    gross = _to_money(order.total_price)
    commission = _to_money(gross * rate)
    net = _to_money(gross - commission)

    async with transaction_scope(session):
        # Re-check inside the write txn to keep idempotency under races.
        existing = await session.scalar(
            select(AgencyEarning).where(AgencyEarning.order_id == order_id)
        )
        if existing is not None:
            return existing
        earning = AgencyEarning(
            order_id=order_id,
            seller_id=seller_id,
            gross=gross,
            commission_rate=rate,
            commission_amount=commission,
            net=net,
            status=AgencyEarningStatus.EARNED,
        )
        session.add(earning)
    await _add_agency_balance(session, seller_id, net)
    return earning


async def reverse_order_earning(session: AsyncSession, order_id: int) -> bool:
    """Reverse an earning if its order is refunded/canceled. Debits the
    agency's earnings balance (never below zero). Idempotent."""
    async with transaction_scope(session):
        earning = await session.scalar(
            select(AgencyEarning).where(AgencyEarning.order_id == order_id)
        )
        if earning is None or earning.status is AgencyEarningStatus.REVERSED:
            return False
        earning.status = AgencyEarningStatus.REVERSED
        seller_id = earning.seller_id
        net = earning.net

    async with transaction_scope(session):
        key = _agency_balance_key(seller_id)
        row = await session.get(AppSetting, key)
        current = Decimal("0.00") if row is None else Decimal(row.value)
        updated = _to_money(max(Decimal("0.00"), current - net))
        if row is None:
            session.add(AppSetting(key=key, value=str(updated)))
        else:
            row.value = str(updated)
    return True


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
        if await is_user_blocked(session, payload.user_id):
            raise UserPermanentlyBlockedError(payload.user_id)
        if not user.is_active:
            raise UserInactiveError(payload.user_id)

        product = await session.get(Product, payload.product_id)
        if product is None or not product.is_active:
            raise ProductNotFoundError(payload.product_id)
        # Marketplace: block sales of a product whose agency is no longer
        # approved (suspended/pending) — a single chokepoint that protects
        # the bot, website and API at once.
        if product.owner_id is not None:
            owner = await session.get(User, product.owner_id)
            if owner is None or owner.agency_status != "approved":
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


async def buy_one_with_wallet(
    session: AsyncSession, user_id: int, product_id: int
) -> Order:
    """One-tap purchase: charge wallet, allocate one stock item, deliverable order.

    This flow is intended for prepaid customers after top-up.
    """
    async with transaction_scope(session):
        user = await session.get(User, user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        if await is_user_blocked(session, user_id):
            raise UserPermanentlyBlockedError(user_id)
        if not user.is_active:
            raise UserInactiveError(user_id)

        product = await session.get(Product, product_id)
        if product is None or not product.is_active:
            raise ProductNotFoundError(product_id)

        await spend_user_balance(session, user_id, product.price)

        order = await create_order_and_allocate_stock(
            session,
            OrderCreate(user_id=user_id, product_id=product_id, quantity=1),
        )
        order.status = OrderStatus.DELIVERED
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
    # Settle the marketplace commission split (no-op for house products).
    await record_order_earning(session, order_id)
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
    # If this order had already earned commission, reverse it (defensive —
    # today only PENDING orders cancel, but keeps the ledger correct if a
    # post-delivery refund path is added later).
    await reverse_order_earning(session, order_id)
    return order


async def list_user_orders(
    session: AsyncSession, user_id: int, limit: int = 5
) -> list[Order]:
    """A customer's most recent orders (for the bot's My Orders view)."""
    async with transaction_scope(session):
        result = await session.scalars(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
        )
        return list(result.all())


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


# --------------------------------------------------------------------------- #
# Marketplace: agency onboarding, governance, reporting
# --------------------------------------------------------------------------- #
class AgencyRow(NamedTuple):
    user: User
    orders: int
    gross: Decimal
    commission: Decimal  # platform's cut
    net: Decimal  # agency's earned total
    balance: Decimal  # current withdrawable


async def apply_as_agency(
    session: AsyncSession, user_id: int, agency_name: str, payout_contact: str
) -> User:
    """A logged-in user requests to become a selling agency (pending)."""
    name = agency_name.strip()
    if len(name) < 2:
        raise ServiceError("Agency name must be at least 2 characters")
    async with transaction_scope(session):
        user = await session.get(User, user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        if user.agency_status == "approved":
            raise ServiceError("You are already an approved agency")
        user.agency_name = name
        user.payout_contact = payout_contact.strip()
        user.agency_status = "pending"
    return user


async def set_agency_status(
    session: AsyncSession, user_id: int, status: str
) -> User:
    """Admin: approve / suspend / reject an agency."""
    if status not in ("approved", "suspended", "pending", "rejected"):
        raise ServiceError(f"Invalid agency status: {status}")
    async with transaction_scope(session):
        user = await session.get(User, user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        user.agency_status = status
        user.is_agency = status == "approved"
    return user


async def is_approved_agency(session: AsyncSession, user_id: int) -> bool:
    async with transaction_scope(session):
        user = await session.get(User, user_id)
    return bool(user and user.agency_status == "approved" and user.is_agency)


async def list_agencies(session: AsyncSession) -> list[AgencyRow]:
    """Every agency (any status) with its lifetime marketplace economics."""
    async with transaction_scope(session):
        users = list(
            await session.scalars(
                select(User)
                .where(User.agency_status.is_not(None))
                .order_by(User.id.desc())
            )
        )
        rows: list[AgencyRow] = []
        for u in users:
            agg = (
                await session.execute(
                    select(
                        func.count(AgencyEarning.id),
                        func.coalesce(func.sum(AgencyEarning.gross), 0),
                        func.coalesce(func.sum(AgencyEarning.commission_amount), 0),
                        func.coalesce(func.sum(AgencyEarning.net), 0),
                    ).where(
                        AgencyEarning.seller_id == u.id,
                        AgencyEarning.status == AgencyEarningStatus.EARNED,
                    )
                )
            ).one()
            balance = await get_agency_balance(session, u.id)
            rows.append(
                AgencyRow(
                    user=u,
                    orders=int(agg[0] or 0),
                    gross=_to_money(Decimal(str(agg[1]))),
                    commission=_to_money(Decimal(str(agg[2]))),
                    net=_to_money(Decimal(str(agg[3]))),
                    balance=balance,
                )
            )
    return rows


async def marketplace_totals(session: AsyncSession) -> dict:
    """Platform-wide marketplace figures for the admin dashboard."""
    async with transaction_scope(session):
        agg = (
            await session.execute(
                select(
                    func.count(AgencyEarning.id),
                    func.coalesce(func.sum(AgencyEarning.gross), 0),
                    func.coalesce(func.sum(AgencyEarning.commission_amount), 0),
                    func.coalesce(func.sum(AgencyEarning.net), 0),
                ).where(AgencyEarning.status == AgencyEarningStatus.EARNED)
            )
        ).one()
        pending = await session.scalar(
            select(func.count(User.id)).where(User.agency_status == "pending")
        )
        approved = await session.scalar(
            select(func.count(User.id)).where(User.agency_status == "approved")
        )
    return {
        "orders": int(agg[0] or 0),
        "gross": _to_money(Decimal(str(agg[1]))),
        "commission": _to_money(Decimal(str(agg[2]))),  # platform revenue
        "net": _to_money(Decimal(str(agg[3]))),  # paid/owed to agencies
        "pending_agencies": int(pending or 0),
        "approved_agencies": int(approved or 0),
    }


# --------------------------------------------------------------------------- #
# Marketplace: agency-scoped product management (agencies touch only their own)
# --------------------------------------------------------------------------- #
class AgencyProductError(ServiceError):
    """Raised when an agency acts on a product it does not own."""


async def _assert_agency_owns(
    session: AsyncSession, owner_id: int, product_id: int
) -> Product:
    async with transaction_scope(session):
        product = await session.get(Product, product_id)
    if product is None:
        raise ProductNotFoundError(product_id)
    if product.owner_id != owner_id:
        raise AgencyProductError("This product does not belong to you")
    return product


async def list_agency_product_overviews(
    session: AsyncSession, owner_id: int
) -> list[ProductOverview]:
    """Per-product stock/sales overview scoped to one agency's products."""
    available = func.count(Inventory.id).filter(
        Inventory.status == InventoryStatus.AVAILABLE
    )
    sold = func.count(Inventory.id).filter(
        Inventory.status == InventoryStatus.SOLD
    )
    stmt = (
        select(Product, available, sold)
        .outerjoin(Inventory, Inventory.product_id == Product.id)
        .where(Product.owner_id == owner_id)
        .group_by(Product.id)
        .order_by(Product.id.desc())
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


async def create_agency_product(
    session: AsyncSession, owner_id: int, payload: ProductCreate
) -> Product:
    """Publish a product on behalf of an approved agency."""
    if not await is_approved_agency(session, owner_id):
        raise AgencyProductError("Your agency is not approved to sell")
    return await create_product(session, payload, owner_id=owner_id)


async def agency_add_inventory(
    session: AsyncSession, owner_id: int, product_id: int, items: list[str]
) -> InventoryImportResult:
    """Add stock to the agency's OWN product (ownership enforced)."""
    await _assert_agency_owns(session, owner_id, product_id)
    return await bulk_add_inventory_with_report(session, product_id, items)


async def agency_update_price(
    session: AsyncSession, owner_id: int, product_id: int, price: Decimal
) -> Product:
    await _assert_agency_owns(session, owner_id, product_id)
    return await update_product_price(session, product_id, price)


async def agency_set_active(
    session: AsyncSession, owner_id: int, product_id: int, is_active: bool
) -> Product:
    await _assert_agency_owns(session, owner_id, product_id)
    return await set_product_active(session, product_id, is_active)


async def get_product_owner_name(
    session: AsyncSession, product: Product
) -> str | None:
    """Display name of a product's selling agency, or None for house."""
    if product.owner_id is None:
        return None
    async with transaction_scope(session):
        owner = await session.get(User, product.owner_id)
    if owner is None or owner.agency_status != "approved":
        return None
    return owner.agency_name or None


# --------------------------------------------------------------------------- #
# Marketplace: payouts (agency withdrawals against earnings balance)
# --------------------------------------------------------------------------- #
MIN_PAYOUT = Decimal("1.00")


async def _spend_agency_balance(
    session: AsyncSession, user_id: int, amount: Decimal
) -> Decimal:
    """Deduct from an agency's earnings balance; raise if insufficient."""
    async with transaction_scope(session):
        key = _agency_balance_key(user_id)
        row = await session.get(AppSetting, key)
        current = Decimal("0.00") if row is None else Decimal(row.value)
        if current < amount:
            raise InsufficientBalanceError(user_id, amount, _to_money(current))
        updated = _to_money(current - amount)
        if row is None:
            session.add(AppSetting(key=key, value=str(updated)))
        else:
            row.value = str(updated)
        return updated


async def request_payout(
    session: AsyncSession, seller_id: int, amount: Decimal, method: str = ""
) -> Payout:
    """Agency requests a withdrawal. Reserves the amount from their balance
    immediately so it cannot be requested twice."""
    amount = _to_money(amount)
    if amount < MIN_PAYOUT:
        raise ServiceError(f"Minimum payout is ${MIN_PAYOUT:.2f}")
    if not await is_approved_agency(session, seller_id):
        raise AgencyProductError("Only approved agencies can request payouts")
    # Deduct first (raises InsufficientBalanceError if the balance is too low).
    await _spend_agency_balance(session, seller_id, amount)
    async with transaction_scope(session):
        user = await session.get(User, seller_id)
        payout = Payout(
            seller_id=seller_id,
            amount=amount,
            method=method.strip() or (user.payout_contact if user else "") or "",
            status=PayoutStatus.REQUESTED,
        )
        session.add(payout)
    return payout


async def set_payout_status(
    session: AsyncSession, payout_id: int, status: str, note: str = ""
) -> Payout:
    """Admin resolves a payout. REJECTED refunds the reserved amount."""
    from datetime import datetime, timezone

    if status not in ("paid", "rejected"):
        raise ServiceError("Payout can only be marked paid or rejected")
    async with transaction_scope(session):
        payout = await session.get(Payout, payout_id)
        if payout is None:
            raise ServiceError(f"Payout {payout_id} not found")
        if payout.status is not PayoutStatus.REQUESTED:
            return payout  # idempotent — already resolved
        payout.status = PayoutStatus(status)
        payout.admin_note = note.strip()
        payout.resolved_at = datetime.now(timezone.utc)
        seller_id = payout.seller_id
        amount = payout.amount
        refund = status == "rejected"
    if refund:
        await _add_agency_balance(session, seller_id, amount)
    return payout


async def list_payouts(
    session: AsyncSession, seller_id: int | None = None, limit: int = 200
) -> list[Payout]:
    stmt = select(Payout).order_by(Payout.created_at.desc(), Payout.id.desc())
    if seller_id is not None:
        stmt = stmt.where(Payout.seller_id == seller_id)
    async with transaction_scope(session):
        return list(await session.scalars(stmt.limit(limit)))


# --------------------------------------------------------------------------- #
# Marketplace: seller trust (sales volume) + storefront visibility
# --------------------------------------------------------------------------- #
async def agency_sales_count(session: AsyncSession, seller_id: int) -> int:
    """Completed (non-reversed) sales for an agency — a real trust signal."""
    async with transaction_scope(session):
        n = await session.scalar(
            select(func.count(AgencyEarning.id)).where(
                AgencyEarning.seller_id == seller_id,
                AgencyEarning.status == AgencyEarningStatus.EARNED,
            )
        )
    return int(n or 0)


def seller_trust_label(sales: int) -> str:
    """Honest, sales-based trust tier shown to buyers."""
    if sales >= 100:
        return f"⭐ Top seller · {sales} sales"
    if sales >= 25:
        return f"⭐ Trusted · {sales} sales"
    if sales >= 1:
        return f"{sales} sales"
    return "New seller"


async def product_visibility(
    session: AsyncSession, product: Product
) -> tuple[bool, str | None, int]:
    """(visible, seller_name, sales_count) for storefront listing.

    House products (no owner) always show with no seller. Agency products
    show ONLY while their agency is approved — a suspended/pending agency's
    products are hidden from buyers automatically.
    """
    if product.owner_id is None:
        return True, None, 0
    async with transaction_scope(session):
        owner = await session.get(User, product.owner_id)
    if owner is None or owner.agency_status != "approved":
        return False, None, 0
    sales = await agency_sales_count(session, owner.id)
    return True, owner.agency_name, sales
