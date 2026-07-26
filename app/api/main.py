"""FastAPI application — HTTP interface over the shared service layer.

Run standalone with:
    uvicorn app.api.main:app --reload
or together with the bot via:
    python run_all.py
"""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from shared import payment_service, services
from shared.config import settings
from shared.database import engine, get_db, init_db
from shared.schemas import (
    OrderCreate,
    OrderResponse,
    PaymentCreate,
    PaymentSessionResponse,
    ProductCreate,
    ProductResponse,
    UserResponse,
    UserStatusUpdate,
    UserUpsert,
)

SessionDep = Annotated[AsyncSession, Depends(get_db)]

# Keep strong references to in-process poll tasks so they aren't GC'd.
_poll_tasks: set[asyncio.Task[bool]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Dev convenience only — in production, manage schema with Alembic.
    await init_db()
    # Background sweeper: settles any SMS orders left WAITING (refunds on
    # timeout) so a restart can never strand a customer's money.
    sweep_task: asyncio.Task | None = None
    if settings.sms_enabled:
        from shared.sms_service import sweep_waiting_orders

        sweep_task = asyncio.create_task(sweep_waiting_orders())
    yield
    if sweep_task is not None:
        sweep_task.cancel()
    for task in _poll_tasks:
        task.cancel()
    await engine.dispose()


app = FastAPI(
    title="Bondom Account API",
    version="2.0.0",
    lifespan=lifespan,
    # Internal schema is not published in production; the developer-facing
    # documentation lives at /developer/docs.
    docs_url="/docs" if settings.enable_internal_docs else None,
    redoc_url="/redoc" if settings.enable_internal_docs else None,
    openapi_url="/openapi.json" if settings.enable_internal_docs else None,
)


# --------------------------------------------------------------------------- #
# Legacy internal API guard
# --------------------------------------------------------------------------- #
def require_internal(
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    """Gate the pre-existing unauthenticated JSON endpoints.

    These routes (/users, /products, /orders, /payments) accept writes and
    had no authentication at all — they were reachable by anyone who could
    talk to the port, with only an nginx rule standing in the way. Nothing
    in the codebase calls them, so they now fail closed: without
    INTERNAL_API_TOKEN they do not exist, and with it they require the
    matching header. 404 rather than 401 so their existence is not
    advertised.
    """
    expected = settings.internal_api_token
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_internal_token or not hmac.compare_digest(
        x_internal_token, expected
    ):
        raise HTTPException(status_code=404, detail="Not Found")


InternalOnly = Depends(require_internal)

# Customer web storefront — HTML pages sharing the same service layer.
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from app.webshop.routes import router as webshop_router

app.include_router(webshop_router)
app.mount(
    "/web/static",
    StaticFiles(directory=str(Path(__file__).parent.parent / "webshop" / "static")),
    name="webshop-static",
)

# --------------------------------------------------------------------------- #
# Public API v1 — API-key authenticated, intentionally reachable from the
# internet (unlike the internal endpoints below, which nginx blocks).
# --------------------------------------------------------------------------- #
import time as _time

from app.api.v1.deps import ApiError, error_response, new_request_id
from app.api.v1.routes import router as api_v1_router
from shared import api_keys as _api_keys
from shared.database import AsyncSessionLocal as _Session

app.include_router(api_v1_router)

# Developer portal (HTML, Telegram-session auth) lives on the storefront.
from app.webshop.developer import router as developer_router

app.include_router(developer_router)

from app.webshop.legal import router as legal_router

app.include_router(legal_router)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    response = error_response(exc, getattr(request.state, "request_id", ""))
    if exc.code == "rate_limit_exceeded":
        response.headers["Retry-After"] = str(exc.extra.get("retry_after", 60))
    return response


@app.middleware("http")
async def api_v1_observability(request: Request, call_next):
    """Attach a request id, rate-limit headers, and log API usage.

    Only /api/v1 traffic is touched; the storefront is left alone so page
    rendering pays nothing for this.
    """
    if not request.url.path.startswith("/api/v1"):
        return await call_next(request)

    request.state.request_id = new_request_id()
    started = _time.perf_counter()
    response = await call_next(request)
    duration_ms = int((_time.perf_counter() - started) * 1000)

    response.headers["X-Request-Id"] = request.state.request_id
    response.headers["X-API-Version"] = "v1"

    rl = getattr(request.state, "rate_limit", None)
    if rl is not None:
        response.headers["X-RateLimit-Limit"] = str(rl.limit)
        response.headers["X-RateLimit-Remaining"] = str(rl.remaining)
        response.headers["X-RateLimit-Reset"] = str(rl.reset_in)

    key_id = getattr(request.state, "api_key_id", None)
    user_id = getattr(request.state, "api_user_id", None)
    if key_id is not None:
        # Usage accounting must never break the response the client gets.
        try:
            async with _Session() as session:
                await _api_keys.touch_key(session, key_id)
                await _api_keys.log_request(
                    session,
                    api_key_id=key_id,
                    user_id=user_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                    ip=request.headers.get(
                        "x-real-ip", request.client.host if request.client else ""
                    ),
                )
        except Exception:
            logging.getLogger(__name__).exception("API usage logging failed")

    return response


# --------------------------------------------------------------------------- #
# Domain exception -> HTTP response mapping
# --------------------------------------------------------------------------- #
@app.exception_handler(services.OutOfStockError)
async def out_of_stock_handler(
    request: Request, exc: services.OutOfStockError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": str(exc),
            "product_id": exc.product_id,
            "requested": exc.requested,
            "available": exc.available,
        },
    )


@app.exception_handler(services.UserInactiveError)
@app.exception_handler(services.UserPermanentlyBlockedError)
async def user_inactive_handler(
    request: Request, exc: services.ServiceError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)}
    )


@app.exception_handler(payment_service.PaymentError)
async def payment_error_handler(
    request: Request, exc: payment_service.PaymentError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)}
    )


@app.exception_handler(services.ProductNotFoundError)
@app.exception_handler(services.UserNotFoundError)
@app.exception_handler(services.OrderNotFoundError)
async def not_found_handler(
    request: Request, exc: services.ServiceError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)}
    )


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
@app.post(
    "/users/",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["users"],
    dependencies=[InternalOnly],
)
async def upsert_user(payload: UserUpsert, db: SessionDep) -> UserResponse:
    """Create or refresh a user record keyed by Telegram id."""
    user = await services.get_or_create_user(
        db, payload.telegram_id, payload.username
    )
    return UserResponse.model_validate(user)


@app.patch(
    "/users/{user_id}/status",
    response_model=UserResponse,
    tags=["users"],
    dependencies=[InternalOnly],
)
async def set_user_status(
    user_id: int, payload: UserStatusUpdate, db: SessionDep
) -> UserResponse:
    """Suspend or reactivate a user (admin control interface)."""
    user = await services.toggle_user_status(db, user_id, payload.is_active)
    return UserResponse.model_validate(user)


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
@app.post(
    "/products/",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["products"],
    dependencies=[InternalOnly],
)
async def create_product(payload: ProductCreate, db: SessionDep) -> ProductResponse:
    """Create a new product in the catalog."""
    product = await services.create_product(db, payload)
    return ProductResponse.model_validate(product)


@app.get(
    "/products/",
    response_model=list[ProductResponse],
    tags=["products"],
    dependencies=[InternalOnly],
)
async def list_products(db: SessionDep, only_active: bool = True) -> list[ProductResponse]:
    products = await services.list_products(db, only_active=only_active)
    return [ProductResponse.model_validate(p) for p in products]


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
@app.post(
    "/orders/",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["orders"],
    dependencies=[InternalOnly],
)
async def create_order(payload: OrderCreate, db: SessionDep) -> OrderResponse:
    """Create an order and atomically allocate inventory to it.

    404 if the user/product is unknown, 403 if the user is suspended,
    409 if there is not enough available stock.
    """
    order = await services.create_order_and_allocate_stock(db, payload)
    return OrderResponse.model_validate(order)


# --------------------------------------------------------------------------- #
# Payments (Bakong KHQR)
# --------------------------------------------------------------------------- #
@app.post(
    "/payments/create",
    response_model=PaymentSessionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    dependencies=[InternalOnly],
)
async def create_payment(payload: PaymentCreate, db: SessionDep) -> PaymentSessionResponse:
    """Start a KHQR payment session for a pending order.

    Generates the QR, stores its md5 for verification, and launches an
    in-process background task that polls Bakong until the payment is
    confirmed or the QR expires (15 min).
    """
    payment = await payment_service.create_payment_session(db, payload.order_id)

    task = asyncio.create_task(
        payment_service.poll_payment_until_paid(payment.id)
    )
    _poll_tasks.add(task)
    task.add_done_callback(_poll_tasks.discard)

    return PaymentSessionResponse.model_validate(payment)


@app.post(
    "/payments/{order_id}/check",
    response_model=OrderResponse,
    tags=["payments"],
    dependencies=[InternalOnly],
)
async def check_payment(order_id: int, db: SessionDep) -> OrderResponse:
    """Manually verify the latest payment session for an order.

    Returns the order (now ``paid``) on success, 402 if not yet paid.
    """
    payment = await payment_service.get_latest_payment(db, order_id)
    if payment is None:
        raise payment_service.PaymentError(
            f"No payment session exists for order {order_id}"
        )

    if await payment_service.verify_payment(payment.md5):
        await payment_service.confirm_payment(db, payment.id)
        order = await services.get_order_with_items(db, order_id)
        return OrderResponse.model_validate(order)

    return JSONResponse(  # type: ignore[return-value]
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        content={"detail": f"Payment for order {order_id} not received yet"},
    )
