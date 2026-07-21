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

    # Marketplace: a user may also be a selling agency (admin-approved).
    is_agency: Mapped[bool] = mapped_column(Boolean, default=False)
    agency_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # agency_status: None | "pending" | "approved" | "suspended"
    agency_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payout_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)

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
    # Marketplace: NULL = house product (platform keeps 100%); set = the
    # agency (user id) that sells it and earns net-of-commission.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

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


class AgencyEarningStatus(str, enum.Enum):
    EARNED = "earned"
    REVERSED = "reversed"


class AgencyEarning(Base):
    """Commission split recorded when an agency's order is delivered.

    ``gross`` is what the buyer paid; ``net`` is the agency's take after the
    platform's ``commission_amount``. One row per order (unique). Reversed
    (status flipped, balance debited) if the order is ever refunded.
    """

    __tablename__ = "agency_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), unique=True, index=True
    )
    seller_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    gross: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    commission_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    net: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[AgencyEarningStatus] = mapped_column(
        Enum(
            AgencyEarningStatus,
            name="agency_earning_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=AgencyEarningStatus.EARNED,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
