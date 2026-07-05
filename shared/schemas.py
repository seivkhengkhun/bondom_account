"""Pydantic schemas for request validation and response serialization."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from shared.models import InventoryStatus, OrderStatus, PaymentStatus

Price = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
class UserUpsert(BaseModel):
    telegram_id: Annotated[int, Field(gt=0)]
    username: Annotated[str | None, Field(max_length=128)] = None


class UserStatusUpdate(BaseModel):
    is_active: bool


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    username: str | None
    is_active: bool
    created_at: datetime


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
class ProductCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    price: Price
    category: Annotated[str, Field(min_length=1, max_length=100)]
    warranty_days: Annotated[int, Field(ge=0)] = 0
    is_active: bool = True


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    price: Decimal
    category: str
    warranty_days: int
    is_active: bool


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
class InventoryItem(BaseModel):
    """Inventory row as delivered to a buyer inside an order response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    data: str
    status: InventoryStatus


class InventoryBulkUpload(BaseModel):
    product_id: Annotated[int, Field(gt=0)]
    items: Annotated[list[str], Field(min_length=1, max_length=10_000)]


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
class OrderCreate(BaseModel):
    user_id: Annotated[int, Field(gt=0)]
    product_id: Annotated[int, Field(gt=0)]
    quantity: Annotated[int, Field(ge=1)] = 1


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    total_price: Decimal
    status: OrderStatus
    created_at: datetime
    items: list[InventoryItem]


# --------------------------------------------------------------------------- #
# Payments
# --------------------------------------------------------------------------- #
class PaymentCreate(BaseModel):
    order_id: Annotated[int, Field(gt=0)]


class PaymentSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    qr_string: str
    md5: str
    amount: Decimal
    currency: str
    status: PaymentStatus
    expires_at: datetime
