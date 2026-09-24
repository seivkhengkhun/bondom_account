"""Pandora Digital supplier integration.

The local database is the customer-facing source of truth. Pandora is read by
scheduled/manual synchronization and immediately before checkout; supplier
orders are created only after the existing Bondom payment has been confirmed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.database import AsyncSessionLocal
from shared.models import (
    Inventory,
    InventoryStatus,
    Order,
    OrderStatus,
    Product,
    SupplierFulfillment,
    SupplierFulfillmentStatus,
    SupplierPricingMode,
    SupplierProduct,
    SupplierSettings,
    SupplierSyncRun,
    SupplierWebhookEvent,
    User,
)

logger = logging.getLogger(__name__)
SUPPLIER = "pandora"
MONEY = Decimal("0.01")
WEBHOOK_MAX_AGE_SECONDS = 300


class PandoraError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 0,
        retry_after: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = details or {}
        super().__init__(message)


class MinimumProfitError(PandoraError):
    pass


@dataclass(frozen=True)
class PandoraBalance:
    available: Decimal
    reserved: Decimal
    currency: str


@dataclass(frozen=True)
class PandoraProductData:
    id: str
    name: str
    description: str
    unit_price: Decimal
    currency: str
    available_stock: int | None
    minimum_quantity: int
    maximum_quantity: int
    delivery_type: str
    updated_at: datetime | None


@dataclass(frozen=True)
class PandoraQuote:
    quote_id: str
    product_id: str
    quantity: int
    unit_price: Decimal
    total_amount: Decimal
    currency: str
    available_stock: int | None
    can_purchase: bool
    price_version: str


@dataclass(frozen=True)
class PandoraOrderData:
    id: str
    status: str
    product_id: str
    quantity: int
    unit_price: Decimal
    total_amount: Decimal
    currency: str
    items: tuple[str, ...]
    failure_code: str
    failure_message: str


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class PandoraClient:
    """Small async client matching Pandora's published v1 contract."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key if api_key is not None else settings.pandora_api_key
        self.base_url = (base_url or settings.pandora_api_base_url).rstrip("/")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str = "",
        retry_rate_limit: bool = True,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise PandoraError("NOT_CONFIGURED", "Pandora API key is not configured")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    params=params,
                    json=body,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PandoraError(
                "NETWORK_ERROR",
                "Pandora did not confirm the request; its state must be reconciled.",
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise PandoraError(
                "INVALID_RESPONSE",
                "Pandora returned an invalid response.",
                status_code=response.status_code,
            ) from exc

        if response.is_success and isinstance(payload, dict):
            return payload

        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        code = str(error.get("code") or f"HTTP_{response.status_code}")
        message = str(error.get("message") or "Pandora request failed")
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        retry_after = 0
        try:
            retry_after = max(0, int(response.headers.get("Retry-After", "0")))
        except ValueError:
            pass
        if response.status_code == 429 and retry_rate_limit and retry_after <= 30:
            await asyncio.sleep(max(1, retry_after))
            return await self._request(
                method,
                path,
                params=params,
                body=body,
                idempotency_key=idempotency_key,
                retry_rate_limit=False,
            )
        raise PandoraError(
            code,
            message,
            status_code=response.status_code,
            retry_after=retry_after,
            details=details,
        )

    async def get_balance(self) -> PandoraBalance:
        data = await self._request("GET", "/balance")
        return PandoraBalance(
            _decimal(data["available_balance"]),
            _decimal(data["reserved_balance"]),
            str(data["currency"]),
        )

    @staticmethod
    def _product(data: dict[str, Any]) -> PandoraProductData:
        return PandoraProductData(
            id=str(data["id"]),
            name=str(data["name"]),
            description=str(data.get("description") or ""),
            unit_price=_decimal(data["unit_price"]),
            currency=str(data["currency"]),
            available_stock=(
                int(data["available_stock"])
                if data.get("available_stock") is not None
                else None
            ),
            minimum_quantity=int(data.get("minimum_quantity", 1)),
            maximum_quantity=int(data["maximum_quantity"]),
            delivery_type=str(data["delivery_type"]),
            updated_at=_dt(data.get("updated_at")),
        )

    async def list_all_products(self) -> list[PandoraProductData]:
        products: list[PandoraProductData] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/products", params=params)
            items = data.get("items")
            if not isinstance(items, list):
                raise PandoraError("INVALID_RESPONSE", "Pandora catalog has no items list")
            products.extend(self._product(item) for item in items)
            next_cursor = data.get("next_cursor")
            if not next_cursor:
                break
            cursor = str(next_cursor)
            if cursor in seen:
                raise PandoraError("INVALID_CURSOR", "Pandora repeated a pagination cursor")
            seen.add(cursor)
        return products

    async def get_product(self, product_id: str) -> PandoraProductData:
        data = await self._request("GET", f"/products/{product_id}")
        return self._product(data)

    async def create_quote(self, product_id: str, quantity: int) -> PandoraQuote:
        data = await self._request(
            "POST", "/quotes", body={"product_id": product_id, "quantity": quantity}
        )
        return PandoraQuote(
            quote_id=str(data["quote_id"]),
            product_id=str(data["product_id"]),
            quantity=int(data["quantity"]),
            unit_price=_decimal(data["unit_price"]),
            total_amount=_decimal(data["total_amount"]),
            currency=str(data["currency"]),
            available_stock=(
                int(data["available_stock"])
                if data.get("available_stock") is not None
                else None
            ),
            can_purchase=bool(data["can_purchase"]),
            price_version=str(data["price_version"]),
        )

    @staticmethod
    def _order(data: dict[str, Any]) -> PandoraOrderData:
        delivery = data.get("delivery") or {}
        items = delivery.get("items") if isinstance(delivery, dict) else []
        return PandoraOrderData(
            id=str(data["id"]),
            status=str(data["status"]),
            product_id=str(data["product_id"]),
            quantity=int(data["quantity"]),
            unit_price=_decimal(data["unit_price"]),
            total_amount=_decimal(data["total_amount"]),
            currency=str(data["currency"]),
            items=tuple(str(item) for item in (items or [])),
            failure_code=str(data.get("failure_code") or ""),
            failure_message=str(data.get("failure_message") or ""),
        )

    async def create_order(
        self, body: dict[str, Any], idempotency_key: str
    ) -> PandoraOrderData:
        data = await self._request(
            "POST", "/orders", body=body, idempotency_key=idempotency_key
        )
        return self._order(data)

    async def get_order(self, order_id: str) -> PandoraOrderData:
        return self._order(await self._request("GET", f"/orders/{order_id}"))

    async def get_webhook(self) -> dict[str, Any]:
        return await self._request("GET", "/webhook")

    async def configure_webhook(self, url: str) -> dict[str, Any]:
        return await self._request("PUT", "/webhook", body={"url": url})


async def get_supplier_settings(session: AsyncSession) -> SupplierSettings:
    row = await session.get(SupplierSettings, SUPPLIER)
    if row is None:
        row = SupplierSettings(
            supplier=SUPPLIER,
            sync_interval_minutes=max(1, settings.pandora_sync_interval_minutes),
            auto_fulfillment_enabled=settings.pandora_auto_fulfillment_enabled,
        )
        session.add(row)
        await session.flush()
    return row


def calculate_retail_price(
    supplier_price: Decimal,
    config: SupplierSettings,
    mode: SupplierPricingMode = SupplierPricingMode.GLOBAL_DEFAULT,
    value: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    selected = mode.value if isinstance(mode, SupplierPricingMode) else str(mode)
    if selected == SupplierPricingMode.GLOBAL_DEFAULT.value:
        selected = config.global_pricing_method
        value = (
            config.default_fixed_profit
            if selected == SupplierPricingMode.FIXED_PROFIT.value
            else config.default_percentage_markup
        )
    value = Decimal(value or 0)
    if selected == SupplierPricingMode.FIXED_PROFIT.value:
        retail = supplier_price + value
    elif selected == SupplierPricingMode.PERCENTAGE_MARKUP.value:
        retail = supplier_price * (Decimal("1") + value / Decimal("100"))
    elif selected == SupplierPricingMode.MANUAL_RETAIL_PRICE.value:
        retail = value
    else:
        raise ValueError(f"Unknown pricing mode: {selected}")
    retail = _money(retail)
    return retail, _money(retail - supplier_price)


_catalog_sync_lock = asyncio.Lock()


async def sync_catalog(client: PandoraClient | None = None) -> SupplierSyncRun:
    """Serialize scheduled/manual syncs so their completion tokens cannot race."""
    async with _catalog_sync_lock:
        return await _sync_catalog_once(client)


async def _sync_catalog_once(client: PandoraClient | None = None) -> SupplierSyncRun:
    """Fetch every page first, then atomically apply a complete catalog."""
    client = client or PandoraClient()
    token = uuid.uuid4().hex
    async with AsyncSessionLocal() as session:
        run = SupplierSyncRun(supplier=SUPPLIER, sync_token=token)
        session.add(run)
        await session.commit()
        run_id = run.id
    try:
        catalog = await client.list_all_products()
        now = datetime.now(timezone.utc)
        created = updated_count = 0
        async with AsyncSessionLocal() as session:
            config = await get_supplier_settings(session)
            for source in catalog:
                mapping = await session.scalar(
                    select(SupplierProduct).where(
                        SupplierProduct.supplier == SUPPLIER,
                        SupplierProduct.supplier_product_id == source.id,
                    )
                )
                if mapping is None:
                    retail, profit = calculate_retail_price(source.unit_price, config)
                    product = Product(
                        name=source.name,
                        price=retail,
                        category="Pandora",
                        warranty_days=0,
                        is_active=True,
                    )
                    session.add(product)
                    await session.flush()
                    mapping = SupplierProduct(
                        product_id=product.id,
                        supplier=SUPPLIER,
                        supplier_product_id=source.id,
                        supplier_name=source.name,
                        supplier_description=source.description,
                        supplier_unit_price=source.unit_price,
                        available_stock=source.available_stock,
                        minimum_quantity=source.minimum_quantity,
                        maximum_quantity=source.maximum_quantity,
                        currency=source.currency,
                        delivery_type=source.delivery_type,
                        supplier_available=True,
                        blocked_below_min_profit=profit < config.minimum_gross_profit,
                        supplier_updated_at=source.updated_at,
                        last_successful_sync=now,
                        last_seen_sync=token,
                    )
                    session.add(mapping)
                    created += 1
                else:
                    product = await session.get(Product, mapping.product_id)
                    mapping.supplier_name = source.name
                    mapping.supplier_description = source.description
                    mapping.supplier_unit_price = source.unit_price
                    mapping.available_stock = source.available_stock
                    mapping.minimum_quantity = source.minimum_quantity
                    mapping.maximum_quantity = source.maximum_quantity
                    mapping.currency = source.currency
                    mapping.delivery_type = source.delivery_type
                    mapping.supplier_available = True
                    mapping.supplier_updated_at = source.updated_at
                    mapping.last_successful_sync = now
                    mapping.last_seen_sync = token
                    if product is not None:
                        retail, profit = calculate_retail_price(
                            source.unit_price,
                            config,
                            mapping.pricing_mode,
                            mapping.pricing_value,
                        )
                        product.price = retail
                        mapping.blocked_below_min_profit = (
                            profit < config.minimum_gross_profit
                        )
                    updated_count += 1

            # Only a successfully completed full pagination pass can make a
            # missing product unavailable. Transient API failures never do.
            missing = list(
                await session.scalars(
                    select(SupplierProduct).where(
                        SupplierProduct.supplier == SUPPLIER,
                        SupplierProduct.last_seen_sync != token,
                    )
                )
            )
            for mapping in missing:
                mapping.supplier_available = False

            run = await session.get(SupplierSyncRun, run_id)
            if run:
                run.status = "success"
                run.products_seen = len(catalog)
                run.products_created = created
                run.products_updated = updated_count
                run.completed_at = now
            await session.commit()
            return run
    except Exception as exc:
        async with AsyncSessionLocal() as session:
            run = await session.get(SupplierSyncRun, run_id)
            if run:
                run.status = "failed"
                run.error_message = str(exc)[:2000]
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()
        raise


async def refresh_product(
    session: AsyncSession, mapping: SupplierProduct, client: PandoraClient | None = None
) -> SupplierProduct:
    source = await (client or PandoraClient()).get_product(mapping.supplier_product_id)
    config = await get_supplier_settings(session)
    retail, profit = calculate_retail_price(
        source.unit_price, config, mapping.pricing_mode, mapping.pricing_value
    )
    product = await session.get(Product, mapping.product_id)
    mapping.supplier_name = source.name
    mapping.supplier_description = source.description
    mapping.supplier_unit_price = source.unit_price
    mapping.available_stock = source.available_stock
    mapping.minimum_quantity = source.minimum_quantity
    mapping.maximum_quantity = source.maximum_quantity
    mapping.currency = source.currency
    mapping.delivery_type = source.delivery_type
    mapping.supplier_available = True
    mapping.blocked_below_min_profit = profit < config.minimum_gross_profit
    mapping.supplier_updated_at = source.updated_at
    mapping.last_successful_sync = datetime.now(timezone.utc)
    if product:
        product.price = retail
    await session.commit()
    return mapping


async def get_mapping_by_product(
    session: AsyncSession, product_id: int
) -> SupplierProduct | None:
    return await session.scalar(
        select(SupplierProduct).where(SupplierProduct.product_id == product_id)
    )


async def save_global_settings(
    session: AsyncSession,
    *,
    method: str,
    fixed_profit: Decimal,
    percentage_markup: Decimal,
    minimum_profit: Decimal,
    sync_interval_minutes: int,
    auto_fulfillment_enabled: bool,
) -> SupplierSettings:
    if method not in (
        SupplierPricingMode.FIXED_PROFIT.value,
        SupplierPricingMode.PERCENTAGE_MARKUP.value,
    ):
        raise ValueError("Global method must be fixed profit or percentage markup")
    if min(fixed_profit, percentage_markup, minimum_profit) < 0:
        raise ValueError("Pricing values cannot be negative")
    config = await get_supplier_settings(session)
    config.global_pricing_method = method
    config.default_fixed_profit = fixed_profit
    config.default_percentage_markup = percentage_markup
    config.minimum_gross_profit = minimum_profit
    config.sync_interval_minutes = max(1, min(int(sync_interval_minutes), 1440))
    config.auto_fulfillment_enabled = bool(auto_fulfillment_enabled)
    mappings = list(await session.scalars(select(SupplierProduct)))
    for mapping in mappings:
        product = await session.get(Product, mapping.product_id)
        if product is None:
            continue
        retail, profit = calculate_retail_price(
            mapping.supplier_unit_price,
            config,
            mapping.pricing_mode,
            mapping.pricing_value,
        )
        product.price = retail
        mapping.blocked_below_min_profit = profit < minimum_profit
    await session.commit()
    return config


async def save_product_settings(
    session: AsyncSession,
    product_id: int,
    *,
    name: str,
    description: str,
    category: str,
    pricing_mode: str,
    pricing_value: Decimal | None,
    enabled: bool,
) -> SupplierProduct:
    mapping = await get_mapping_by_product(session, product_id)
    product = await session.get(Product, product_id)
    if mapping is None or product is None:
        raise ValueError("Pandora product mapping not found")
    try:
        mode = SupplierPricingMode(pricing_mode)
    except ValueError as exc:
        raise ValueError("Invalid product pricing mode") from exc
    if mode is not SupplierPricingMode.GLOBAL_DEFAULT:
        if pricing_value is None or pricing_value < 0:
            raise ValueError("This pricing mode requires a non-negative value")
    config = await get_supplier_settings(session)
    retail, profit = calculate_retail_price(
        mapping.supplier_unit_price, config, mode, pricing_value
    )
    if retail <= 0:
        raise ValueError("Retail price must be greater than zero")
    product.name = name.strip()[:255] or mapping.supplier_name
    product.category = category.strip()[:100] or "Pandora"
    product.price = retail
    product.is_active = enabled
    mapping.customer_description = description.strip() or None
    mapping.pricing_mode = mode
    mapping.pricing_value = (
        None if mode is SupplierPricingMode.GLOBAL_DEFAULT else pricing_value
    )
    mapping.blocked_below_min_profit = profit < config.minimum_gross_profit
    await session.commit()
    return mapping


async def admin_snapshot(session: AsyncSession) -> dict[str, Any]:
    config = await get_supplier_settings(session)
    mappings = list(
        await session.scalars(
            select(SupplierProduct).order_by(SupplierProduct.product_id)
        )
    )
    rows = []
    for mapping in mappings:
        product = await session.get(Product, mapping.product_id)
        if product is None:
            continue
        _, profit = calculate_retail_price(
            mapping.supplier_unit_price,
            config,
            mapping.pricing_mode,
            mapping.pricing_value,
        )
        rows.append((mapping, product, profit))
    fulfillments = list(
        await session.scalars(
            select(SupplierFulfillment).order_by(SupplierFulfillment.id.desc()).limit(200)
        )
    )
    last_sync = await session.scalar(
        select(SupplierSyncRun)
        .where(SupplierSyncRun.status == "success")
        .order_by(SupplierSyncRun.id.desc())
        .limit(1)
    )
    return {
        "settings": config,
        "products": rows,
        "fulfillments": fulfillments,
        "last_sync": last_sync,
    }


async def prepare_fulfillment(
    session: AsyncSession,
    order: Order,
    mapping: SupplierProduct,
    quantity: int,
    client: PandoraClient | None = None,
) -> SupplierFulfillment:
    quote = await (client or PandoraClient()).create_quote(
        mapping.supplier_product_id, quantity
    )
    if not quote.can_purchase:
        raise PandoraError("PRODUCT_UNAVAILABLE", "Pandora cannot fulfill this quantity")
    config = await get_supplier_settings(session)
    retail, profit = calculate_retail_price(
        quote.unit_price, config, mapping.pricing_mode, mapping.pricing_value
    )
    if profit < config.minimum_gross_profit:
        mapping.blocked_below_min_profit = True
        raise MinimumProfitError(
            "MINIMUM_PROFIT",
            "This product is temporarily unavailable while its price is reviewed.",
        )
    if quantity < mapping.minimum_quantity or quantity > mapping.maximum_quantity:
        raise PandoraError(
            "QUANTITY_NOT_ALLOWED",
            f"Quantity must be between {mapping.minimum_quantity} and {mapping.maximum_quantity}.",
        )
    if quote.available_stock is not None and quote.available_stock < quantity:
        raise PandoraError("OUT_OF_STOCK", "Pandora stock is below the requested quantity")

    mapping.supplier_unit_price = quote.unit_price
    mapping.available_stock = quote.available_stock
    mapping.blocked_below_min_profit = False
    product = await session.get(Product, mapping.product_id)
    if product:
        product.price = retail
    order.total_price = retail * quantity
    idempotency_key = f"bondom-order-{order.id}-{uuid.uuid4().hex[:20]}"
    body = {
        "product_id": mapping.supplier_product_id,
        "quantity": quantity,
        "expected_unit_price": str(quote.unit_price),
        "price_version": quote.price_version,
        "client_order_reference": f"bondom-order-{order.id}",
    }
    fulfillment = SupplierFulfillment(
        order_id=order.id,
        supplier_product_id=mapping.id,
        quantity=quantity,
        retail_unit_price=retail,
        quoted_supplier_unit_price=quote.unit_price,
        quote_id=quote.quote_id,
        price_version=quote.price_version,
        idempotency_key=idempotency_key,
        request_body=json.dumps(body, separators=(",", ":"), sort_keys=True),
        status=SupplierFulfillmentStatus.AWAITING_PAYMENT,
    )
    session.add(fulfillment)
    return fulfillment


async def is_supplier_order(session: AsyncSession, order_id: int) -> bool:
    return bool(
        await session.scalar(
            select(func.count()).select_from(SupplierFulfillment).where(
                SupplierFulfillment.order_id == order_id
            )
        )
    )


async def _apply_supplier_order(
    session: AsyncSession,
    fulfillment: SupplierFulfillment,
    remote: PandoraOrderData,
) -> SupplierFulfillment:
    fulfillment.supplier_order_id = remote.id
    fulfillment.failure_code = remote.failure_code
    fulfillment.failure_message = remote.failure_message
    try:
        fulfillment.status = SupplierFulfillmentStatus(remote.status)
    except ValueError:
        fulfillment.status = SupplierFulfillmentStatus.REVIEW
        fulfillment.failure_message = f"Unknown Pandora status: {remote.status}"
        await session.commit()
        return fulfillment

    if remote.status == "delivered":
        if len(remote.items) != fulfillment.quantity:
            fulfillment.status = SupplierFulfillmentStatus.REVIEW
            fulfillment.failure_code = "DELIVERY_QUANTITY_MISMATCH"
            fulfillment.failure_message = (
                f"Expected {fulfillment.quantity} items, received {len(remote.items)}"
            )
            await session.commit()
            return fulfillment
        existing = await session.scalar(
            select(func.count()).select_from(Inventory).where(
                Inventory.assigned_order_id == fulfillment.order_id
            )
        )
        if not existing:
            mapping = await session.get(SupplierProduct, fulfillment.supplier_product_id)
            if mapping is None:
                raise PandoraError("LOCAL_MAPPING_MISSING", "Supplier mapping is missing")
            session.add_all(
                Inventory(
                    product_id=mapping.product_id,
                    data=item,
                    status=InventoryStatus.SOLD,
                    assigned_order_id=fulfillment.order_id,
                )
                for item in remote.items
            )
            await session.flush()  # credentials durable before order delivery state
        order = await session.get(Order, fulfillment.order_id)
        if order:
            order.status = OrderStatus.DELIVERED
    await session.commit()
    return fulfillment


async def fulfill_paid_order(
    session: AsyncSession,
    order_id: int,
    client: PandoraClient | None = None,
    *,
    force: bool = False,
) -> SupplierFulfillment | None:
    fulfillment = await session.scalar(
        select(SupplierFulfillment).where(SupplierFulfillment.order_id == order_id)
    )
    if fulfillment is None:
        return None
    if fulfillment.status is SupplierFulfillmentStatus.DELIVERED:
        return fulfillment
    order = await session.get(Order, order_id)
    if order is None or order.status not in (OrderStatus.PAID, OrderStatus.DELIVERED):
        return fulfillment
    config = await get_supplier_settings(session)
    if not (force or config.auto_fulfillment_enabled):
        if fulfillment.status is SupplierFulfillmentStatus.AWAITING_PAYMENT:
            fulfillment.status = SupplierFulfillmentStatus.READY
            await session.commit()
        return fulfillment

    client = client or PandoraClient()
    try:
        if fulfillment.supplier_order_id:
            remote = await client.get_order(fulfillment.supplier_order_id)
        else:
            body = json.loads(fulfillment.request_body)
            if fulfillment.status in (
                SupplierFulfillmentStatus.AWAITING_PAYMENT,
                SupplierFulfillmentStatus.READY,
            ):
                fulfillment.status = SupplierFulfillmentStatus.PENDING
                await session.commit()  # claim/request intent is durable first
            remote = await client.create_order(body, fulfillment.idempotency_key)
        return await _apply_supplier_order(session, fulfillment, remote)
    except PandoraError as exc:
        # A network timeout is ambiguous: retain PENDING and retry the same
        # body/key during reconciliation. All explicit business failures need
        # administrator review and are never blindly requoted.
        if exc.code != "NETWORK_ERROR":
            fulfillment.status = SupplierFulfillmentStatus.REVIEW
            fulfillment.failure_code = exc.code
            fulfillment.failure_message = str(exc)
            await session.commit()
        raise


async def admin_resolve_fulfillment(
    session: AsyncSession,
    order_id: int,
    client: PandoraClient | None = None,
) -> SupplierFulfillment | None:
    """Administrator-controlled retry/requote for a paid supplier order.

    A definitively rejected PRICE_CHANGED request may receive a fresh body and
    key only after the admin asks for resolution. Ambiguous timeouts retain the
    original body/key through the normal fulfillment path.
    """
    client = client or PandoraClient()
    fulfillment = await session.scalar(
        select(SupplierFulfillment).where(SupplierFulfillment.order_id == order_id)
    )
    if fulfillment is None:
        return None
    if fulfillment.supplier_order_id and fulfillment.status in (
        SupplierFulfillmentStatus.FAILED,
        SupplierFulfillmentStatus.REFUNDED,
        SupplierFulfillmentStatus.REVIEW,
    ):
        raise PandoraError(
            "ADMIN_REVIEW_REQUIRED",
            "The supplier has a terminal order; do not create a replacement automatically.",
        )
    if (
        fulfillment.status is SupplierFulfillmentStatus.REVIEW
        and fulfillment.failure_code == "PRICE_CHANGED"
    ):
        mapping = await session.get(SupplierProduct, fulfillment.supplier_product_id)
        if mapping is None:
            raise PandoraError("LOCAL_MAPPING_MISSING", "Supplier mapping is missing")
        quote = await client.create_quote(
            mapping.supplier_product_id, fulfillment.quantity
        )
        config = await get_supplier_settings(session)
        actual_profit = _money(fulfillment.retail_unit_price - quote.unit_price)
        if actual_profit < config.minimum_gross_profit:
            raise MinimumProfitError(
                "MINIMUM_PROFIT",
                "The paid order cannot be purchased at the new cost without violating minimum profit.",
            )
        body = {
            "product_id": mapping.supplier_product_id,
            "quantity": fulfillment.quantity,
            "expected_unit_price": str(quote.unit_price),
            "price_version": quote.price_version,
            "client_order_reference": f"bondom-order-{order_id}",
        }
        fulfillment.quote_id = quote.quote_id
        fulfillment.quoted_supplier_unit_price = quote.unit_price
        fulfillment.price_version = quote.price_version
        fulfillment.request_body = json.dumps(
            body, separators=(",", ":"), sort_keys=True
        )
        fulfillment.idempotency_key = (
            f"bondom-order-{order_id}-retry-{uuid.uuid4().hex[:14]}"
        )
        fulfillment.status = SupplierFulfillmentStatus.READY
        fulfillment.failure_code = ""
        fulfillment.failure_message = ""
        await session.commit()
    return await fulfill_paid_order(session, order_id, client, force=True)


async def reconcile_fulfillments(client: PandoraClient | None = None) -> int:
    client = client or PandoraClient()
    async with AsyncSessionLocal() as session:
        rows = list(
            await session.scalars(
                select(SupplierFulfillment).where(
                    SupplierFulfillment.status.in_(
                        [
                            SupplierFulfillmentStatus.READY,
                            SupplierFulfillmentStatus.PENDING,
                            SupplierFulfillmentStatus.PROCESSING,
                            SupplierFulfillmentStatus.DELIVERED,
                        ]
                    )
                )
            )
        )
        ids = [row.order_id for row in rows]
    changed = 0
    for order_id in ids:
        try:
            async with AsyncSessionLocal() as session:
                before = await session.scalar(
                    select(SupplierFulfillment.status).where(
                        SupplierFulfillment.order_id == order_id
                    )
                )
                row = await fulfill_paid_order(session, order_id, client)
                if row and row.status != before:
                    changed += 1
                should_notify = bool(
                    row
                    and row.status is SupplierFulfillmentStatus.DELIVERED
                    and row.customer_notified_at is None
                )
            if should_notify:
                await notify_customer_delivery(order_id)
        except PandoraError:
            logger.warning("Pandora reconciliation deferred for order %s", order_id)
        await asyncio.sleep(0)
    return changed


async def notify_customer_delivery(order_id: int) -> bool:
    """Best-effort Telegram delivery after asynchronous reconciliation.

    Database delivery is already complete before this runs, so a Telegram
    outage cannot hide the credentials from authenticated order history.
    """
    if not settings.bot_token:
        return False
    from aiogram import Bot
    from app.bot.handlers import _deliver_order_to_chat
    from shared import services

    async with AsyncSessionLocal() as session:
        fulfillment = await session.scalar(
            select(SupplierFulfillment).where(
                SupplierFulfillment.order_id == order_id
            )
        )
        order = await services.get_order_with_items(session, order_id)
        user = await session.get(User, order.user_id)
        if fulfillment is None or user is None or fulfillment.customer_notified_at:
            return False
        telegram_id = int(user.telegram_id)
    bot = Bot(settings.bot_token)
    try:
        await _deliver_order_to_chat(
            bot, telegram_id, order, title="✅ Supplier order delivered"
        )
    except Exception:
        logger.exception("Telegram delivery failed for Pandora order %s", order_id)
        return False
    finally:
        await bot.session.close()
    async with AsyncSessionLocal() as session:
        fulfillment = await session.scalar(
            select(SupplierFulfillment).where(
                SupplierFulfillment.order_id == order_id
            )
        )
        if fulfillment and fulfillment.customer_notified_at is None:
            fulfillment.customer_notified_at = datetime.now(timezone.utc)
            await session.commit()
    return True


def verify_webhook_signature(
    raw_body: bytes,
    timestamp: str,
    signature_header: str,
    secret: str,
    *,
    now: float | None = None,
) -> bool:
    if not secret or not timestamp or not signature_header:
        return False
    try:
        stamp = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - stamp) > WEBHOOK_MAX_AGE_SECONDS:
        return False
    digest = hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    expected = f"v1={digest}"
    return any(
        hmac.compare_digest(expected, candidate.strip())
        for candidate in signature_header.split(",")
    )


async def persist_webhook_event(
    session: AsyncSession,
    event_id: str,
    event_type: str,
    raw_body: bytes,
) -> tuple[SupplierWebhookEvent, bool]:
    existing = await session.scalar(
        select(SupplierWebhookEvent).where(SupplierWebhookEvent.event_id == event_id)
    )
    if existing:
        return existing, False
    row = SupplierWebhookEvent(
        supplier=SUPPLIER,
        event_id=event_id,
        event_type=event_type[:64],
        raw_payload=raw_body.decode("utf-8", errors="replace"),
    )
    session.add(row)
    await session.commit()  # acknowledge only after durable acceptance
    return row, True


async def mark_webhook_processed(event_id: str) -> None:
    async with AsyncSessionLocal() as session:
        row = await session.scalar(
            select(SupplierWebhookEvent).where(SupplierWebhookEvent.event_id == event_id)
        )
        if row:
            row.processed = True
            row.processed_at = datetime.now(timezone.utc)
            await session.commit()


async def synchronization_loop() -> None:
    await asyncio.sleep(10)
    while True:
        try:
            await sync_catalog()
        except Exception:
            logger.exception("Pandora catalog synchronization failed")
        async with AsyncSessionLocal() as session:
            config = await get_supplier_settings(session)
            interval = max(1, config.sync_interval_minutes)
        await asyncio.sleep(interval * 60)


async def reconciliation_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await reconcile_fulfillments()
        except Exception:
            logger.exception("Pandora fulfillment reconciliation failed")
        await asyncio.sleep(60)
