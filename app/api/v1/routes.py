"""Public API v1.

Everything here is authenticated with an API key and scoped to the key's
owner: a caller can only ever see and spend their own account. Purchases
draw on the owner's wallet balance, which is the same balance used on the
website and in the bot.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from shared import services, sms_service
from shared.models import OrderStatus

from .deps import ApiError, CallerDep, SessionDep

router = APIRouter(prefix="/api/v1", tags=["public-api"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ProductOut(BaseModel):
    id: int
    name: str
    category: str
    price: str
    warranty_days: int
    in_stock: int


class BalanceOut(BaseModel):
    user_id: int
    username: str
    balance: str
    currency: str = "USD"


class OrderCreateIn(BaseModel):
    product_id: int = Field(..., ge=1)
    quantity: int = Field(1, ge=1, le=50)


class OrderOut(BaseModel):
    id: int
    status: str
    total_price: str
    created_at: str
    items: list[str] = []


class SmsCountryOut(BaseModel):
    country: str
    code: str
    price: str
    success_rate: float | None = None


class SmsOrderIn(BaseModel):
    service: str = Field(..., description="facebook or instagram")
    country_code: str


class SmsOrderOut(BaseModel):
    id: int
    service: str
    country: str
    phone: str
    status: str
    price: str
    code: str = ""


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
@router.get("/ping", summary="Health check (no auth)")
async def ping() -> dict:
    return {"ok": True, "version": "v1"}


@router.get("/me", response_model=BalanceOut, summary="Your account")
async def me(caller: CallerDep, db: SessionDep) -> BalanceOut:
    caller.require("read")
    balance = await services.get_user_balance(db, caller.user.id)
    return BalanceOut(
        user_id=caller.user.id,
        username=caller.user.username or "",
        balance=f"{balance:.2f}",
    )


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
@router.get("/products", response_model=list[ProductOut], summary="List products")
async def list_products(
    caller: CallerDep,
    db: SessionDep,
    category: str | None = Query(None),
    in_stock_only: bool = Query(False),
) -> list[ProductOut]:
    caller.require("read")
    overviews = await services.list_product_overviews(db)
    out = []
    for o in overviews:
        if not o.product.is_active:
            continue
        if category and o.product.category.lower() != category.lower():
            continue
        if in_stock_only and o.available <= 0:
            continue
        out.append(
            ProductOut(
                id=o.product.id,
                name=o.product.name,
                category=o.product.category,
                price=f"{o.product.price:.2f}",
                warranty_days=o.product.warranty_days,
                in_stock=o.available,
            )
        )
    return out


@router.get(
    "/products/{product_id}", response_model=ProductOut, summary="Get one product"
)
async def get_product(
    product_id: int, caller: CallerDep, db: SessionDep
) -> ProductOut:
    caller.require("read")
    for o in await services.list_product_overviews(db):
        if o.product.id == product_id and o.product.is_active:
            return ProductOut(
                id=o.product.id,
                name=o.product.name,
                category=o.product.category,
                price=f"{o.product.price:.2f}",
                warranty_days=o.product.warranty_days,
                in_stock=o.available,
            )
    raise ApiError("product_not_found", f"No product with id {product_id}.", 404)


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
def _order_out(order, items: list[str] | None = None) -> OrderOut:
    return OrderOut(
        id=order.id,
        status=order.status.value,
        total_price=f"{order.total_price:.2f}",
        created_at=order.created_at.isoformat() if order.created_at else "",
        items=items or [],
    )


@router.post(
    "/orders",
    response_model=OrderOut,
    status_code=201,
    summary="Buy a product with your wallet balance",
)
async def create_order(
    payload: OrderCreateIn, caller: CallerDep, db: SessionDep
) -> OrderOut:
    """Purchases are paid from the key owner's wallet.

    Top up at ``/wallet`` on the website; there is no card flow in the
    API. Returns 402 when the balance is short.
    """
    caller.require("orders")

    delivered: list[str] = []
    try:
        for _ in range(payload.quantity):
            order = await services.buy_one_with_wallet(
                db, caller.user.id, payload.product_id
            )
            full = await services.get_order_with_items(db, order.id)
            for item in full.items:
                if getattr(item, "inventory", None) is not None:
                    delivered.append(item.inventory.payload)
    except services.InsufficientBalanceError as exc:
        raise ApiError(
            "insufficient_balance",
            (
                f"Wallet balance ${exc.balance:.2f} is short of "
                f"${exc.required:.2f}."
            ),
            status=402,
            required=f"{exc.required:.2f}",
            balance=f"{exc.balance:.2f}",
        ) from exc
    except services.OutOfStockError as exc:
        raise ApiError(
            "out_of_stock",
            f"Only {exc.available} left for product {exc.product_id}.",
            status=409,
            available=exc.available,
            requested=exc.requested,
        ) from exc
    except services.ProductNotFoundError as exc:
        raise ApiError("product_not_found", str(exc), 404) from exc
    except services.UserPermanentlyBlockedError as exc:
        raise ApiError("account_blocked", str(exc), 403) from exc

    return _order_out(order, delivered)


@router.get("/orders", response_model=list[OrderOut], summary="Your orders")
async def list_orders(
    caller: CallerDep, db: SessionDep, limit: int = Query(50, ge=1, le=200)
) -> list[OrderOut]:
    caller.require("read")
    orders = await services.list_user_orders(db, caller.user.id, limit=limit)
    return [_order_out(o) for o in orders]


@router.get("/orders/{order_id}", response_model=OrderOut, summary="Get one order")
async def get_order(order_id: int, caller: CallerDep, db: SessionDep) -> OrderOut:
    caller.require("read")
    try:
        order = await services.get_order_with_items(db, order_id)
    except services.OrderNotFoundError as exc:
        raise ApiError("order_not_found", str(exc), 404) from exc

    if order.user_id != caller.user.id:
        # Same response as a genuine miss, so ids cannot be probed.
        raise ApiError("order_not_found", f"No order with id {order_id}.", 404)

    items = [
        i.inventory.payload
        for i in order.items
        if getattr(i, "inventory", None) is not None
    ] if order.status in (OrderStatus.PAID, OrderStatus.DELIVERED) else []
    return _order_out(order, items)


# --------------------------------------------------------------------------- #
# SMS activation
# --------------------------------------------------------------------------- #
@router.get(
    "/sms/countries",
    response_model=list[SmsCountryOut],
    summary="Available SMS numbers",
)
async def sms_countries(
    caller: CallerDep, db: SessionDep, service: str = Query("facebook")
) -> list[SmsCountryOut]:
    caller.require("sms")
    try:
        offers = await sms_service.get_stock(service)
    except Exception as exc:  # provider outage
        raise ApiError(
            "provider_unavailable",
            "The SMS provider is not responding right now.",
            status=503,
        ) from exc

    markup = await sms_service.get_markup(db)
    return [
        SmsCountryOut(
            country=o.get("country", ""),
            code=str(o.get("code", "")),
            price=f"{sms_service.sell_price(o.get('cost', 0), markup):.2f}",
            success_rate=o.get("success_rate"),
        )
        for o in offers
    ]


@router.post(
    "/sms/orders",
    response_model=SmsOrderOut,
    status_code=201,
    summary="Rent an SMS number",
)
async def create_sms_order(
    payload: SmsOrderIn, caller: CallerDep, db: SessionDep
) -> SmsOrderOut:
    caller.require("sms")
    if payload.service not in ("facebook", "instagram"):
        raise ApiError(
            "invalid_service", "service must be 'facebook' or 'instagram'.", 400
        )
    try:
        order = await sms_service.create_sms_order(
            db, caller.user.id, payload.service, payload.country_code
        )
    except services.InsufficientBalanceError as exc:
        raise ApiError(
            "insufficient_balance",
            f"Wallet balance ${exc.balance:.2f} is short of ${exc.required:.2f}.",
            status=402,
        ) from exc
    except Exception as exc:
        raise ApiError("sms_order_failed", str(exc), 400) from exc

    return SmsOrderOut(
        id=order.id,
        service=order.category,
        country=order.country,
        phone=order.phone,
        status=order.status.value,
        price=f"{order.price:.2f}",
        code=order.otp_code or "",
    )


@router.get(
    "/sms/orders/{sms_id}",
    response_model=SmsOrderOut,
    summary="Check an SMS order for its code",
)
async def get_sms_order(
    sms_id: int, caller: CallerDep, db: SessionDep
) -> SmsOrderOut:
    caller.require("sms")
    order = await db.get(sms_service.SmsOrder, sms_id)
    if order is None or order.user_id != caller.user.id:
        raise ApiError("sms_order_not_found", f"No SMS order with id {sms_id}.", 404)
    return SmsOrderOut(
        id=order.id,
        service=order.category,
        country=order.country,
        phone=order.phone,
        status=order.status.value,
        price=f"{order.price:.2f}",
        code=order.otp_code or "",
    )
