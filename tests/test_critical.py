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
