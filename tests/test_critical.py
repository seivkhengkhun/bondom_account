"""Tests for the paths where a bug costs money or leaks access.

Deliberately narrow: wallet arithmetic, top-up limits, API key lifecycle,
admin credentials, CSRF, and user deletion. Everything runs against a
throwaway SQLite file, never the real database.

Run with:  python -m pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import api_keys, audit, services  # noqa: E402
from shared.admin_auth import (  # noqa: E402
    hash_password,
    validate_new_password,
    verify_hash,
)
from shared.database import Base  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from shared.models import Order, User  # noqa: E402
from shared.payment_service import (  # noqa: E402
    MAX_TOPUP,
    MIN_TOPUP,
    topup_amount_error,
)
from app.webshop.auth import check_csrf, csrf_token, read_session, sign_session  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def db():
    """A fresh throwaway database per test."""
    path = Path(tempfile.gettempdir()) / f"bondom_test_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    try:
        yield maker
    finally:
        await engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


async def _user(maker, telegram_id: int = 111, name: str = "tester") -> User:
    async with maker() as s:
        return await services.get_or_create_user(s, telegram_id, name)


# --------------------------------------------------------------------------- #
# Wallet arithmetic — a bug here is money
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wallet_credit_and_debit_roundtrip(db):
    user = await _user(db)
    async with db() as s:
        assert await services.get_user_balance(s, user.id) == Decimal("0.00")

    async with db() as s:
        await services.add_user_balance(s, user.id, Decimal("10.00"))
    async with db() as s:
        assert await services.get_user_balance(s, user.id) == Decimal("10.00")

    async with db() as s:
        await services.spend_user_balance(s, user.id, Decimal("2.50"))
    async with db() as s:
        assert await services.get_user_balance(s, user.id) == Decimal("7.50")


@pytest.mark.asyncio
async def test_wallet_cannot_go_negative(db):
    user = await _user(db)
    async with db() as s:
        await services.add_user_balance(s, user.id, Decimal("1.00"))

    async with db() as s:
        with pytest.raises(services.InsufficientBalanceError):
            await services.spend_user_balance(s, user.id, Decimal("5.00"))

    # The failed debit must not have moved anything.
    async with db() as s:
        assert await services.get_user_balance(s, user.id) == Decimal("1.00")


@pytest.mark.asyncio
async def test_wallet_rejects_non_positive_amounts(db):
    user = await _user(db)
    for amount in (Decimal("0"), Decimal("-5")):
        async with db() as s:
            with pytest.raises(services.ServiceError):
                await services.add_user_balance(s, user.id, amount)


# --------------------------------------------------------------------------- #
# Top-up limits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "amount,accepted",
    [
        ("0.01", False),
        ("1.99", False),
        ("2.00", True),
        ("2.01", True),
        ("500.00", True),
        ("500.01", False),
    ],
)
def test_topup_boundaries(amount, accepted):
    assert (topup_amount_error(Decimal(amount)) is None) is accepted


def test_topup_message_names_the_minimum():
    msg = topup_amount_error(Decimal("1.00"))
    assert msg == f"Minimum top-up amount is ${MIN_TOPUP:.0f}."
    assert topup_amount_error(Decimal("999")) == (
        f"Maximum top-up amount is ${MAX_TOPUP:.0f}."
    )


# --------------------------------------------------------------------------- #
# Admin credentials
# --------------------------------------------------------------------------- #
def test_password_hash_roundtrip_and_salting():
    record = hash_password("Str0ng-Passw0rd!")
    assert verify_hash("Str0ng-Passw0rd!", record)
    assert not verify_hash("wrong", record)
    assert not verify_hash("Str0ng-Passw0rd!", "garbage")
    # Salted: the same password must never produce the same record.
    assert hash_password("x") != hash_password("x")
    # The plaintext must not be recoverable from the stored record.
    assert "Str0ng-Passw0rd!" not in record


@pytest.mark.parametrize(
    "password,confirm,ok",
    [
        ("", "", False),
        ("short1A!", "short1A!", False),
        ("alllowercase", "alllowercase", False),
        ("Str0ng-Passw0rd!", "mismatch", False),
        ("password", "password", False),
        ("Str0ng-Passw0rd!", "Str0ng-Passw0rd!", True),
    ],
)
def test_password_policy(password, confirm, ok):
    assert validate_new_password(password, confirm).ok is ok


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_api_key_lifecycle(db):
    user = await _user(db)

    async with db() as s:
        issued = await api_keys.create_key(s, user.id, "test", ["read", "orders"])

    assert issued.plaintext.startswith("bk_live_")

    async with db() as s:
        found = await api_keys.resolve_key(s, issued.plaintext)
        assert found is not None and found.user_id == user.id

    # Revocation must be durable and immediate.
    async with db() as s:
        assert await api_keys.revoke_key(s, user.id, issued.id) is True
    async with db() as s:
        assert await api_keys.resolve_key(s, issued.plaintext) is None
    # Revoking twice is a no-op, not an error.
    async with db() as s:
        assert await api_keys.revoke_key(s, user.id, issued.id) is False


@pytest.mark.asyncio
async def test_api_key_cannot_be_revoked_by_another_user(db):
    owner = await _user(db, 111, "owner")
    other = await _user(db, 222, "other")

    async with db() as s:
        issued = await api_keys.create_key(s, owner.id, "victim")

    async with db() as s:
        assert await api_keys.revoke_key(s, other.id, issued.id) is False
    # Still usable by its rightful owner.
    async with db() as s:
        assert await api_keys.resolve_key(s, issued.plaintext) is not None


@pytest.mark.asyncio
async def test_api_key_stored_only_as_hash(db):
    user = await _user(db)
    async with db() as s:
        issued = await api_keys.create_key(s, user.id, "test")
        rows = await api_keys.list_keys(s, user.id)

    row = next(r for r in rows if r.id == issued.id)
    assert row.key_hash != issued.plaintext
    assert issued.plaintext not in row.key_hash
    assert row.key_hash == api_keys.hash_key(issued.plaintext)


def test_unknown_scopes_are_dropped():
    assert api_keys.normalise_scopes(["read", "root", "sms"]) == "read,sms"
    assert api_keys.normalise_scopes([]) == api_keys.DEFAULT_SCOPES
    assert api_keys.normalise_scopes(["nonsense"]) == api_keys.DEFAULT_SCOPES


# --------------------------------------------------------------------------- #
# Sessions and CSRF
# --------------------------------------------------------------------------- #
def test_session_cookie_rejects_tampering():
    cookie = sign_session(1234, "someone")
    assert read_session(cookie)["tid"] == 1234

    raw, sig = cookie.rsplit(".", 1)
    assert read_session(f"{raw}.{'0' * len(sig)}") is None
    assert read_session("garbage") is None
    assert read_session(None) is None


def test_csrf_token_is_bound_to_the_session():
    a = {"tid": 1, "u": "a"}
    b = {"tid": 2, "u": "b"}
    assert csrf_token(a) != csrf_token(b)
    assert check_csrf(a, csrf_token(a))
    # A token minted for another user must not validate.
    assert not check_csrf(a, csrf_token(b))
    assert not check_csrf(a, "")
    assert not check_csrf(None, csrf_token(a))


# --------------------------------------------------------------------------- #
# User deletion
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hard_delete_refused_when_user_has_orders(db):
    user = await _user(db)
    async with db() as s:
        s.add(Order(user_id=user.id, total_price=Decimal("1.00")))
        await s.commit()

    async with db() as s:
        with pytest.raises(services.UserHasOrdersError):
            await services.delete_user(s, user.id)

    # Both the user and their financial history survive.
    async with db() as s:
        assert await s.get(User, user.id) is not None


@pytest.mark.asyncio
async def test_hard_delete_removes_balance_row(db):
    user = await _user(db)
    async with db() as s:
        await services.add_user_balance(s, user.id, Decimal("5.00"))

    async with db() as s:
        await services.delete_user(s, user.id)

    async with db() as s:
        assert await s.get(User, user.id) is None
        # The balance lives in app_settings, not on the user row — it is
        # easy to leave behind.
        assert await services.get_user_balance(s, user.id) == Decimal("0.00")


@pytest.mark.asyncio
async def test_soft_delete_is_reversible_and_keeps_orders(db):
    user = await _user(db)
    async with db() as s:
        s.add(Order(user_id=user.id, total_price=Decimal("1.00")))
        await s.commit()

    async with db() as s:
        await services.soft_delete_user(s, user.id, reason="spam")

    async with db() as s:
        refreshed = await s.get(User, user.id)
        assert refreshed.is_active is False
        assert await services.is_user_deleted(s, user.id) is True
        assert user.id in await services.list_deleted_user_ids(s)

    async with db() as s:
        await services.restore_user(s, user.id)

    async with db() as s:
        refreshed = await s.get(User, user.id)
        assert refreshed.is_active is True
        assert await services.is_user_deleted(s, user.id) is False


@pytest.mark.asyncio
async def test_suspension_is_not_mistaken_for_deletion(db):
    """A suspended user must not be reported as deleted."""
    user = await _user(db)
    async with db() as s:
        await services.toggle_user_status(s, user.id, False)
    async with db() as s:
        assert await services.is_user_deleted(s, user.id) is False


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_audit_never_raises_on_failure(monkeypatch):
    """An audit failure must not break the action being audited."""

    class Boom:
        def __call__(self, *a, **kw):
            raise RuntimeError("database is gone")

    monkeypatch.setattr(audit, "AsyncSessionLocal", Boom())
    # Must swallow the error rather than propagate it.
    await audit.log_action(audit.ACTION_USER_DELETE, summary="x")


# --------------------------------------------------------------------------- #
# Admin override of the top-up minimum
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_topup_override_is_off_by_default(db):
    user = await _user(db)
    async with db() as s:
        assert await services.has_topup_override(s, user.id) is False


@pytest.mark.asyncio
async def test_topup_override_can_be_granted_and_revoked(db):
    user = await _user(db)
    async with db() as s:
        await services.set_topup_override(s, user.id, True, note="testing")
    async with db() as s:
        assert await services.has_topup_override(s, user.id) is True
        assert user.id in await services.list_topup_override_user_ids(s)

    async with db() as s:
        await services.set_topup_override(s, user.id, False)
    async with db() as s:
        assert await services.has_topup_override(s, user.id) is False
        assert user.id not in await services.list_topup_override_user_ids(s)


@pytest.mark.asyncio
async def test_override_is_per_user_only(db):
    """Granting it to one user must not affect anybody else."""
    a = await _user(db, 111, "granted")
    b = await _user(db, 222, "normal")
    async with db() as s:
        await services.set_topup_override(s, a.id, True)
    async with db() as s:
        assert await services.has_topup_override(s, a.id) is True
        assert await services.has_topup_override(s, b.id) is False


@pytest.mark.asyncio
async def test_override_on_missing_user_raises(db):
    async with db() as s:
        with pytest.raises(services.UserNotFoundError):
            await services.set_topup_override(s, 999999, True)


@pytest.mark.parametrize(
    "amount,override,accepted",
    [
        ("1.00", False, False),   # normal user, below $2
        ("1.00", True, True),     # exempted user, allowed
        ("0.01", True, True),     # exempted user, at the floor
        ("0.00", True, False),    # zero is never a payment
        ("-1.00", True, False),   # negative is never a payment
        ("2.00", False, True),
        ("500.01", True, False),  # the maximum still applies
    ],
)
def test_override_lowers_the_minimum_but_not_the_maximum(
    amount, override, accepted
):
    err = topup_amount_error(Decimal(amount), override=override)
    assert (err is None) is accepted


def test_effective_minimum_values():
    from shared.payment_service import OVERRIDE_MIN_TOPUP, effective_min_topup

    assert effective_min_topup(False) == MIN_TOPUP
    assert effective_min_topup(True) == OVERRIDE_MIN_TOPUP
    # The override lowers the floor, it does not remove it.
    assert OVERRIDE_MIN_TOPUP > 0


# --------------------------------------------------------------------------- #
# Minimum order quantity
# --------------------------------------------------------------------------- #
async def _product(maker, price="1.00"):
    from shared.schemas import ProductCreate
    async with maker() as s:
        return await services.create_product(
            s, ProductCreate(name="p", price=Decimal(price), category="c",
                             warranty_days=0)
        )


@pytest.mark.asyncio
async def test_min_quantity_defaults_to_one(db):
    p = await _product(db)
    async with db() as s:
        assert await services.get_product_min_quantity(s, p.id) == 1


@pytest.mark.asyncio
async def test_min_quantity_set_and_clear(db):
    p = await _product(db)
    async with db() as s:
        assert await services.set_product_min_quantity(s, p.id, 5) == 5
    async with db() as s:
        assert await services.get_product_min_quantity(s, p.id) == 5
        assert (await services.get_product_min_quantities(s))[p.id] == 5

    # Setting it back to 1 removes the row rather than storing the default.
    async with db() as s:
        assert await services.set_product_min_quantity(s, p.id, 1) == 1
    async with db() as s:
        assert await services.get_product_min_quantity(s, p.id) == 1
        assert p.id not in await services.get_product_min_quantities(s)


@pytest.mark.asyncio
async def test_min_quantity_is_clamped(db):
    p = await _product(db)
    async with db() as s:
        assert await services.set_product_min_quantity(s, p.id, 0) == 1
    async with db() as s:
        assert await services.set_product_min_quantity(s, p.id, 10_000) == (
            services.MAX_MIN_ORDER_QTY
        )


@pytest.mark.asyncio
async def test_min_quantity_unknown_product_raises(db):
    async with db() as s:
        with pytest.raises(services.ProductNotFoundError):
            await services.set_product_min_quantity(s, 999999, 3)


@pytest.mark.asyncio
async def test_order_below_minimum_is_refused_and_allocates_nothing(db):
    from shared.models import Inventory, InventoryStatus
    from shared.schemas import OrderCreate

    user = await _user(db)
    p = await _product(db)
    async with db() as s:
        for i in range(5):
            s.add(Inventory(product_id=p.id, data=f"item{i}",
                            status=InventoryStatus.AVAILABLE))
        await s.commit()
    async with db() as s:
        await services.set_product_min_quantity(s, p.id, 3)

    async with db() as s:
        with pytest.raises(services.BelowMinimumQuantityError) as err:
            await services.create_order_and_allocate_stock(
                s, OrderCreate(user_id=user.id, product_id=p.id, quantity=2)
            )
        assert err.value.minimum == 3
        assert err.value.requested == 2

    # Nothing was reserved by the failed attempt.
    async with db() as s:
        available = await s.scalar(
            select(func.count()).select_from(Inventory).where(
                Inventory.product_id == p.id,
                Inventory.status == InventoryStatus.AVAILABLE,
            )
        )
        assert available == 5
        assert await s.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.asyncio
async def test_order_at_the_minimum_succeeds(db):
    from shared.models import Inventory, InventoryStatus
    from shared.schemas import OrderCreate

    user = await _user(db)
    p = await _product(db)
    async with db() as s:
        for i in range(5):
            s.add(Inventory(product_id=p.id, data=f"item{i}",
                            status=InventoryStatus.AVAILABLE))
        await s.commit()
    async with db() as s:
        await services.set_product_min_quantity(s, p.id, 3)

    async with db() as s:
        order = await services.create_order_and_allocate_stock(
            s, OrderCreate(user_id=user.id, product_id=p.id, quantity=3)
        )
        assert order.id is not None


@pytest.mark.asyncio
async def test_wallet_one_tap_buy_respects_the_minimum(db):
    """buy_one_with_wallet buys exactly 1, so a higher minimum blocks it —
    and must not take the money on the way out."""
    from shared.models import Inventory, InventoryStatus

    user = await _user(db)
    p = await _product(db)
    async with db() as s:
        s.add(Inventory(product_id=p.id, data="x",
                        status=InventoryStatus.AVAILABLE))
        await s.commit()
    async with db() as s:
        await services.add_user_balance(s, user.id, Decimal("10.00"))
        await services.set_product_min_quantity(s, p.id, 2)

    async with db() as s:
        with pytest.raises(services.BelowMinimumQuantityError):
            await services.buy_one_with_wallet(s, user.id, p.id)

    async with db() as s:
        assert await services.get_user_balance(s, user.id) == Decimal("10.00")


# --------------------------------------------------------------------------- #
# Payment request rate limiting
# --------------------------------------------------------------------------- #
def test_payment_limit_allows_then_blocks():
    from shared import payment_limits as pl
    pl.reset()
    for i in range(pl.MAX_REQUESTS):
        pl.consume("order", 1, target=f"order:{i}")
    with pytest.raises(pl.PaymentRateLimited) as err:
        pl.consume("order", 1, target="order:last")
    assert err.value.retry_after >= 1
    pl.reset()


def test_payment_limit_blocks_immediate_duplicates():
    from shared import payment_limits as pl
    pl.reset()
    pl.consume("topup", 7, target="topup:5.00")
    with pytest.raises(pl.PaymentRateLimited):
        pl.consume("topup", 7, target="topup:5.00")
    # A different target is fine — it is not a duplicate.
    pl.consume("topup", 7, target="topup:9.00")
    pl.reset()


def test_payment_limit_is_per_user_and_per_kind():
    from shared import payment_limits as pl
    pl.reset()
    for i in range(pl.MAX_REQUESTS):
        pl.consume("order", 1, target=f"o{i}")
    # Another user is unaffected...
    pl.consume("order", 2, target="o0")
    # ...and so is a different kind for the same user, so a burst of
    # top-ups cannot lock someone out of paying for an order.
    pl.consume("topup", 1, target="t0")
    pl.reset()


def test_payment_limit_reset_is_scoped():
    from shared import payment_limits as pl
    pl.reset()
    for i in range(pl.MAX_REQUESTS):
        pl.consume("order", 1, target=f"o{i}")
    pl.reset(kind="order", user_id=1)
    pl.consume("order", 1, target="fresh")
    pl.reset()
