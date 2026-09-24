"""ORM models for the digital product store — shared source of truth."""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.database import Base


class InventoryStatus(str, enum.Enum):
    AVAILABLE = "available"
    SOLD = "sold"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    DELIVERED = "delivered"
    CANCELED = "canceled"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"


class TopupStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"


class SmsOrderStatus(str, enum.Enum):
    WAITING = "waiting_sms"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    FAILED = "failed"


class SupplierPricingMode(str, enum.Enum):
    GLOBAL_DEFAULT = "GLOBAL_DEFAULT"
    FIXED_PROFIT = "FIXED_PROFIT"
    PERCENTAGE_MARKUP = "PERCENTAGE_MARKUP"
    MANUAL_RETAIL_PRICE = "MANUAL_RETAIL_PRICE"


class SupplierFulfillmentStatus(str, enum.Enum):
    AWAITING_PAYMENT = "awaiting_payment"
    READY = "ready"
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    REFUNDED = "refunded"
    REVIEW = "review"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128))
    # Control switch used by the admin panel; the bot refuses purchases
    # from inactive users (see services.create_order_and_allocate_stock).
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    topups: Mapped[list["WalletTopup"]] = relationship(back_populates="user")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    category: Mapped[str] = mapped_column(String(100), index=True)
    warranty_days: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    inventory_items: Mapped[list["Inventory"]] = relationship(
        back_populates="product"
    )


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        # Serves the hot allocation query:
        # "oldest available item for product X", locked FOR UPDATE.
        Index(
            "ix_inventory_product_status_created",
            "product_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT")
    )

    # NOTE(security): field-level encryption goes here.
    # `data` holds the deliverable secret (account credentials, license key,
    # gift-card code, ...). In production wrap this column with an encrypting
    # TypeDecorator (e.g. AES-GCM via `cryptography`, key from KMS/env — never
    # hard-coded) so values are encrypted before hitting the wire and
    # decrypted transparently on load. Plaintext must never be stored at rest.
    data: Mapped[str] = mapped_column(Text)

    status: Mapped[InventoryStatus] = mapped_column(
        Enum(
            InventoryStatus,
            name="inventory_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=InventoryStatus.AVAILABLE,
    )
    assigned_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    product: Mapped["Product"] = relationship(back_populates="inventory_items")
    order: Mapped["Order | None"] = relationship(back_populates="items")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    total_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            name="order_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=OrderStatus.PENDING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="orders")
    items: Mapped[list["Inventory"]] = relationship(back_populates="order")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")


class Payment(Base):
    """One KHQR payment session for an order.

    An order can have several sessions (e.g. the first QR expired), but at
    most one ends up ``paid``. The ``md5`` of the KHQR string is Bakong's
    transaction lookup key and is stored immediately after QR generation.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    md5: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    qr_string: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="payment_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PaymentStatus.PENDING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped["Order"] = relationship(back_populates="payments")


class AppSetting(Base):
    """Simple key/value app settings persisted in the shared database."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class WalletTopup(Base):
    """Wallet top-up KHQR session for a specific user."""

    __tablename__ = "wallet_topups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    md5: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    qr_string: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[TopupStatus] = mapped_column(
        Enum(
            TopupStatus,
            name="topup_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=TopupStatus.PENDING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="topups")


class SmsOrder(Base):
    """A rented SMS-activation phone number (website-only feature).

    ``price`` is what the customer paid (provider cost + markup);
    ``cost`` is what the provider charged our reseller balance. Refunds
    credit the customer's wallet with ``price``.
    """

    __tablename__ = "sms_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(32))
    country: Mapped[str] = mapped_column(String(64))
    country_code: Mapped[str] = mapped_column(String(8))
    phone: Mapped[str] = mapped_column(String(32), default="")
    provider_order_id: Mapped[str] = mapped_column(
        String(64), default="", index=True
    )
    cost: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[SmsOrderStatus] = mapped_column(
        Enum(
            SmsOrderStatus,
            name="sms_order_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SmsOrderStatus.WAITING,
        index=True,
    )
    otp_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# --------------------------------------------------------------------------- #
# Admin activity log
# --------------------------------------------------------------------------- #
class AdminAuditLog(Base):
    """Append-only record of privileged admin actions.

    Anything that destroys data or moves money is written here before or
    immediately after it happens, so there is always an answer to "who
    did this, when, and why".
    """

    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Free-form actor label. Today the panel has a single shared login, so
    # this is "admin"; it becomes meaningful the moment multiple admin
    # accounts exist, without needing a schema change.
    actor: Mapped[str] = mapped_column(String(64), default="admin", index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    target_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
class ApiKey(Base):
    """A third-party integration credential owned by a user.

    Only a SHA-256 hash of the key is stored — the plaintext is shown
    once at creation and can never be recovered, so a database leak does
    not hand over working credentials. ``prefix`` is the short public
    fragment used to identify a key in listings and logs.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64), default="")
    prefix: Mapped[str] = mapped_column(String(24), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Comma-separated scopes, e.g. "read,orders,sms".
    scopes: Mapped[str] = mapped_column(String(128), default="read")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=60)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ApiRequestLog(Base):
    """One row per authenticated API request, for the developer portal."""

    __tablename__ = "api_request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), index=True, nullable=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    method: Mapped[str] = mapped_column(String(8), default="GET")
    path: Mapped[str] = mapped_column(String(255), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=200, index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# --------------------------------------------------------------------------- #
# External suppliers — additive tables only; existing product/order rows stay
# untouched and local-inventory products continue using their original flow.
# --------------------------------------------------------------------------- #
class SupplierProduct(Base):
    """Pandora catalog data mapped permanently to one local Product row.

    Supplier-original fields are kept here. Customer-facing name/category and
    retail price remain on Product, so administrator edits survive every sync.
    A nullable customer_description is the local override; when absent the
    supplier description is displayed.
    """

    __tablename__ = "supplier_products"
    __table_args__ = (
        UniqueConstraint(
            "supplier", "supplier_product_id", name="uq_supplier_product"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), unique=True, index=True
    )
    supplier: Mapped[str] = mapped_column(String(32), default="pandora", index=True)
    supplier_product_id: Mapped[str] = mapped_column(String(100), index=True)
    supplier_name: Mapped[str] = mapped_column(String(255))
    supplier_description: Mapped[str] = mapped_column(Text, default="")
    supplier_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    available_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_quantity: Mapped[int] = mapped_column(Integer, default=1)
    maximum_quantity: Mapped[int] = mapped_column(Integer, default=1)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    delivery_type: Mapped[str] = mapped_column(String(32), default="instant")
    supplier_available: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    pricing_mode: Mapped[SupplierPricingMode] = mapped_column(
        Enum(
            SupplierPricingMode,
            name="supplier_pricing_mode",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SupplierPricingMode.GLOBAL_DEFAULT,
    )
    pricing_value: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    blocked_below_min_profit: Mapped[bool] = mapped_column(Boolean, default=False)
    supplier_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_successful_sync: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_seen_sync: Mapped[str] = mapped_column(String(64), default="", index=True)


class SupplierSettings(Base):
    """Single-row durable pricing and fulfillment configuration."""

    __tablename__ = "supplier_settings"

    supplier: Mapped[str] = mapped_column(String(32), primary_key=True)
    global_pricing_method: Mapped[str] = mapped_column(
        String(32), default="FIXED_PROFIT"
    )
    default_fixed_profit: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("1.00")
    )
    default_percentage_markup: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal("30.00")
    )
    minimum_gross_profit: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0.00")
    )
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    auto_fulfillment_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SupplierFulfillment(Base):
    """Durable, idempotent supplier purchase state for one customer order."""

    __tablename__ = "supplier_fulfillments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), unique=True, index=True
    )
    supplier_product_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_products.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    retail_unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    quoted_supplier_unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    quote_id: Mapped[str] = mapped_column(String(100), default="")
    price_version: Mapped[str] = mapped_column(String(100), default="")
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    request_body: Mapped[str] = mapped_column(Text, default="")
    supplier_order_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True, index=True
    )
    status: Mapped[SupplierFulfillmentStatus] = mapped_column(
        Enum(
            SupplierFulfillmentStatus,
            name="supplier_fulfillment_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SupplierFulfillmentStatus.AWAITING_PAYMENT,
        index=True,
    )
    failure_code: Mapped[str] = mapped_column(String(64), default="")
    failure_message: Mapped[str] = mapped_column(Text, default="")
    customer_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SupplierWebhookEvent(Base):
    __tablename__ = "supplier_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier: Mapped[str] = mapped_column(String(32), default="pandora", index=True)
    event_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="")
    raw_payload: Mapped[str] = mapped_column(Text)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SupplierSyncRun(Base):
    __tablename__ = "supplier_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier: Mapped[str] = mapped_column(String(32), default="pandora", index=True)
    sync_token: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    products_seen: Mapped[int] = mapped_column(Integer, default=0)
    products_created: Mapped[int] = mapped_column(Integer, default=0)
    products_updated: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
