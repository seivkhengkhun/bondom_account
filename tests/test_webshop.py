"""Storefront regression tests — the contracts the website redesign must keep.

These render real pages through the FastAPI app against a throwaway SQLite
database. They pin what other code depends on rather than how pages look:
form field names, CSRF, ownership checks, polling JSON shapes, the Telegram
login callback, and the element hooks the payment-polling scripts use.

No Telegram, Bakong or SMS network call is made: the bot username is
pre-seeded, the bot token is a fake, payments run in dev mode and every
background watcher is replaced.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.main import app  # noqa: E402
from app.bot import handlers  # noqa: E402
from app.webshop import developer, routes  # noqa: E402
from app.webshop import emoji as web_emoji  # noqa: E402
from app.webshop.auth import SESSION_COOKIE, csrf_token, sign_session  # noqa: E402
from shared import payment_limits, payment_service, services  # noqa: E402
from shared.config import settings  # noqa: E402
from shared.database import Base  # noqa: E402
from shared.models import Order, OrderStatus  # noqa: E402
from shared.schemas import OrderCreate, ProductCreate  # noqa: E402

FAKE_TOKEN = "123456:TEST-not-a-real-token"
BOT_USERNAME = "bondom_test_bot"


class Store:
    def __init__(self, maker, client: httpx.AsyncClient):
        self.maker = maker
        self.client = client

    async def user(self, telegram_id: int = 1001, name: str = "buyer"):
        async with self.maker() as s:
            return await services.get_or_create_user(s, telegram_id, name)

    async def product(
        self,
        name: str = "Netflix Premium",
        price: str = "4.50",
        category: str = "streaming",
        stock: int = 3,
        warranty: int = 0,
    ):
        async with self.maker() as s:
            product = await services.create_product(
                s,
                ProductCreate(
                    name=name,
                    price=Decimal(price),
                    category=category,
                    warranty_days=warranty,
                ),
            )
        if stock:
            async with self.maker() as s:
                await services.bulk_add_inventory(
                    s, product.id, [f"{name}-cred-{i}" for i in range(stock)]
                )
        return product

    async def order(self, user_id: int, product_id: int, qty: int = 1) -> Order:
        async with self.maker() as s:
            return await services.create_order_and_allocate_stock(
                s, OrderCreate(user_id=user_id, product_id=product_id, quantity=qty)
            )

    def sign_in(self, telegram_id: int = 1001, name: str = "buyer") -> str:
        cookie = sign_session(telegram_id, name)
        self.client.cookies.set(SESSION_COOKIE, cookie)
        return csrf_token({"tid": telegram_id})

    def sign_out(self) -> None:
        self.client.cookies.clear()


@pytest_asyncio.fixture
async def store(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'shop.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    monkeypatch.setattr(routes, "AsyncSessionLocal", maker)
    monkeypatch.setattr(developer, "AsyncSessionLocal", maker)
    monkeypatch.setattr(web_emoji, "AsyncSessionLocal", maker)
    monkeypatch.setattr(web_emoji, "appearance_cache", web_emoji._WebAppearance())
    monkeypatch.setattr(web_emoji, "CACHE_DIR", tmp_path / "emoji-cache")
    monkeypatch.setattr(web_emoji, "_failures", {})
    monkeypatch.setattr(web_emoji, "_locks", {})

    async def _no_telegram(custom_id):
        raise AssertionError("tests must not reach Telegram")

    monkeypatch.setattr(web_emoji, "_fetch_from_telegram", _no_telegram)
    monkeypatch.setattr(routes, "_bot_username", BOT_USERNAME)
    monkeypatch.setattr(settings, "bot_token", FAKE_TOKEN)
    monkeypatch.setattr(settings, "payment_dev_mode", True)
    monkeypatch.setattr(settings, "sms_enabled", False)

    async def _no_watcher(*args, **kwargs):
        return None

    monkeypatch.setattr(handlers, "_watch_payment_and_auto_deliver", _no_watcher)
    monkeypatch.setattr(payment_service, "poll_wallet_topup_until_paid", _no_watcher)
    payment_limits.reset()

    # 127.0.0.1 is treated as local, so the session cookie is not Secure and
    # the client jar keeps it across requests the way a browser would.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", follow_redirects=False
    ) as client:
        yield Store(maker, client)

    payment_limits.reset()
    await engine.dispose()


def _hidden(html: str, name: str) -> str | None:
    """Value of a named <input> anywhere in the page, or None if absent."""
    m = re.search(
        rf'<input[^>]*name="{re.escape(name)}"[^>]*>', html, flags=re.S
    )
    if not m:
        return None
    v = re.search(r'value="([^"]*)"', m.group(0))
    return v.group(1) if v else ""


def _telegram_params(telegram_id: int = 1001, **extra: str) -> dict[str, str]:
    fields = {
        "id": str(telegram_id),
        "first_name": "Buyer",
        "username": "buyer",
        "auth_date": str(int(time.time())),
        **extra,
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hashlib.sha256(FAKE_TOKEN.encode()).digest()
    fields["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return fields


# --------------------------------------------------------------------------- #
# Public pages render
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_home_lists_active_products_and_offers_telegram_login(store):
    await store.product("Netflix Premium", category="streaming")
    hidden = await store.product("Hidden Thing", category="accounts")
    async with store.maker() as s:
        await services.set_product_active(s, hidden.id, False)

    r = await store.client.get("/")
    assert r.status_code == 200
    assert "Netflix Premium" in r.text
    assert "Hidden Thing" not in r.text
    assert f'data-telegram-login="{BOT_USERNAME}"' in r.text
    assert "/auth/telegram" in r.text


@pytest.mark.asyncio
async def test_product_page_signed_out_shows_login_not_buy_form(store):
    p = await store.product()
    r = await store.client.get(f"/p/{p.id}")
    assert r.status_code == 200
    assert "Netflix Premium" in r.text
    assert 'action="/web/buy"' not in r.text
    assert f'data-telegram-login="{BOT_USERNAME}"' in r.text


@pytest.mark.asyncio
async def test_unknown_or_inactive_product_redirects_home(store):
    r = await store.client.get("/p/9999")
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/legal", "/legal/terms", "/legal/privacy", "/legal/refunds"])
async def test_legal_pages_render(store, path):
    r = await store.client.get(path)
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/my/orders", "/wallet", "/developer"])
async def test_account_pages_require_sign_in(store, path):
    r = await store.client.get(path)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].startswith("/")


@pytest.mark.asyncio
async def test_browser_404_is_branded_html_and_api_404_stays_json(store):
    r = await store.client.get("/no-such-page", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    r = await store.client.get("/no-such-page", headers={"accept": "application/json"})
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_sms_hidden_when_disabled(store):
    r = await store.client.get("/sms")
    assert r.status_code in (302, 303, 307)


# --------------------------------------------------------------------------- #
# Telegram login callback
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_valid_telegram_login_sets_session_and_creates_user(store):
    r = await store.client.get("/auth/telegram", params=_telegram_params(4242))
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/"
    assert SESSION_COOKIE in r.headers.get("set-cookie", "")
    async with store.maker() as s:
        assert await services.get_user_by_telegram_id(s, 4242) is not None


@pytest.mark.asyncio
async def test_forged_telegram_login_is_rejected(store):
    params = _telegram_params(4242)
    params["id"] = "9999"  # tamper after signing
    r = await store.client.get("/auth/telegram", params=params)
    assert r.headers["location"] == "/?login=failed"
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_logout_clears_session(store):
    store.sign_in()
    r = await store.client.get("/logout")
    assert r.status_code in (302, 303, 307)
    assert SESSION_COOKIE in r.headers.get("set-cookie", "")


# --------------------------------------------------------------------------- #
# Purchase flow contract
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_signed_in_product_page_has_buy_form_contract(store):
    await store.user()
    p = await store.product(stock=5)
    token = store.sign_in()

    r = await store.client.get(f"/p/{p.id}")
    assert r.status_code == 200
    html = r.text
    assert 'action="/web/buy"' in html
    assert 'method="post"' in html.lower()
    assert _hidden(html, "product_id") == str(p.id)
    assert _hidden(html, "csrf_token") == token
    assert _hidden(html, "quantity") is not None


@pytest.mark.asyncio
async def test_buy_without_valid_csrf_creates_no_order(store):
    await store.user()
    p = await store.product()
    store.sign_in()
    r = await store.client.post(
        "/web/buy", data={"product_id": p.id, "quantity": 1, "csrf_token": "forged"}
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    async with store.maker() as s:
        user = await services.get_user_by_telegram_id(s, 1001)
        assert await services.list_user_orders(s, user.id) == []


@pytest.mark.asyncio
async def test_buy_signed_out_creates_no_order(store):
    p = await store.product()
    r = await store.client.post(
        "/web/buy", data={"product_id": p.id, "quantity": 1, "csrf_token": "x"}
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/p/{p.id}"


@pytest.mark.asyncio
async def test_buy_creates_pending_order_and_pay_page_polls(store):
    await store.user()
    p = await store.product(price="4.50", stock=5)
    token = store.sign_in()

    r = await store.client.post(
        "/web/buy", data={"product_id": p.id, "quantity": 2, "csrf_token": token}
    )
    assert r.status_code == 303
    m = re.fullmatch(r"/pay/(\d+)", r.headers["location"])
    assert m, r.headers["location"]
    order_id = int(m.group(1))

    async with store.maker() as s:
        order = await services.get_order_with_items(s, order_id)
    assert order.status is OrderStatus.PENDING
    assert order.total_price == Decimal("9.00")
    assert len(order.items) == 2

    r = await store.client.get(f"/pay/{order_id}")
    assert r.status_code == 200
    html = r.text
    assert "data:image/png;base64," in html  # the KHQR itself
    assert "9.00" in html
    for hook in ('id="status"', 'id="expiry"', 'id="expiry-fill"'):
        assert hook in html, hook
    assert f"/web/status/{order_id}" in html

    r = await store.client.get(f"/web/status/{order_id}")
    assert r.status_code == 200
    assert r.json() == {"status": "pending"}


@pytest.mark.asyncio
async def test_out_of_stock_buy_redirects_back_with_error(store):
    await store.user()
    p = await store.product(stock=1)
    token = store.sign_in()
    r = await store.client.post(
        "/web/buy", data={"product_id": p.id, "quantity": 2, "csrf_token": token}
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/p/{p.id}?error=")


@pytest.mark.asyncio
async def test_below_minimum_quantity_is_refused(store):
    await store.user()
    p = await store.product(stock=10)
    async with store.maker() as s:
        await services.set_product_min_quantity(s, p.id, 3)
    token = store.sign_in()

    r = await store.client.get(f"/p/{p.id}")
    assert re.search(r'name="quantity"[^>]*min="3"|min="3"[^>]*name="quantity"', r.text)

    r = await store.client.post(
        "/web/buy", data={"product_id": p.id, "quantity": 1, "csrf_token": token}
    )
    assert r.status_code == 303
    assert "minimum" in r.headers["location"]


# --------------------------------------------------------------------------- #
# Orders: ownership and states
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delivered_order_shows_credentials_to_owner_only(store):
    owner = await store.user(1001, "owner")
    await store.user(2002, "stranger")
    p = await store.product(stock=2)
    order = await store.order(owner.id, p.id, 2)
    async with store.maker() as s:
        await services.mark_order_paid(s, order.id)
        await services.mark_order_delivered(s, order.id)

    store.sign_in(1001, "owner")
    r = await store.client.get(f"/order/{order.id}")
    assert r.status_code == 200
    assert "Netflix Premium-cred-0" in r.text
    assert 'data-copy="Netflix Premium-cred-0"' in r.text

    r = await store.client.get("/my/orders")
    assert r.status_code == 200
    assert f"/order/{order.id}" in r.text

    store.sign_out()
    store.sign_in(2002, "stranger")
    r = await store.client.get(f"/order/{order.id}")
    assert r.status_code in (302, 303, 307)
    assert "Netflix Premium-cred-0" not in r.text
    r = await store.client.get(f"/web/status/{order.id}")
    assert r.status_code == 404
    assert r.json() == {"status": "unknown"}
    r = await store.client.get(f"/pay/{order.id}")
    assert r.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_pending_order_links_back_to_payment(store):
    owner = await store.user()
    p = await store.product()
    order = await store.order(owner.id, p.id)
    store.sign_in()
    r = await store.client.get("/my/orders")
    assert f"/pay/{order.id}" in r.text
    r = await store.client.get(f"/order/{order.id}")
    assert r.status_code == 200
    assert f"/pay/{order.id}" in r.text


@pytest.mark.asyncio
async def test_canceled_order_page_renders(store):
    owner = await store.user()
    p = await store.product()
    order = await store.order(owner.id, p.id)
    async with store.maker() as s:
        await services.cancel_order_and_release_inventory(s, order.id)
    store.sign_in()
    r = await store.client.get(f"/order/{order.id}")
    assert r.status_code == 200
    r = await store.client.get(f"/web/status/{order.id}")
    assert r.json() == {"status": "canceled"}


# --------------------------------------------------------------------------- #
# Wallet contract
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_wallet_page_has_topup_form_contract(store):
    await store.user()
    token = store.sign_in()
    r = await store.client.get("/wallet")
    assert r.status_code == 200
    html = r.text
    assert 'action="/web/wallet/topup"' in html
    assert _hidden(html, "csrf_token") == token
    assert _hidden(html, "amount") is not None
    # The no-refund notice is policy text and must stay on the page.
    assert "not responsible for" in html


@pytest.mark.asyncio
async def test_wallet_topup_flow_and_status_json(store):
    await store.user()
    token = store.sign_in()
    r = await store.client.post(
        "/web/wallet/topup", data={"amount": "5", "csrf_token": token}
    )
    assert r.status_code == 303
    m = re.fullmatch(r"/wallet/pay/(\d+)", r.headers["location"])
    assert m, r.headers["location"]
    topup_id = int(m.group(1))

    r = await store.client.get(f"/wallet/pay/{topup_id}")
    assert r.status_code == 200
    for hook in ('id="status"', 'id="expiry"', 'id="expiry-fill"'):
        assert hook in r.text, hook
    assert f"/web/wallet/status/{topup_id}" in r.text

    r = await store.client.get(f"/web/wallet/status/{topup_id}")
    assert r.json() == {"status": "pending"}

    store.sign_out()
    store.sign_in(2002, "stranger")
    await store.user(2002, "stranger")
    r = await store.client.get(f"/web/wallet/status/{topup_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_wallet_topup_rejects_bad_csrf_and_bad_amounts(store):
    await store.user()
    token = store.sign_in()
    r = await store.client.post(
        "/web/wallet/topup", data={"amount": "5", "csrf_token": "forged"}
    )
    assert r.headers["location"].startswith("/wallet?error=")
    r = await store.client.post(
        "/web/wallet/topup", data={"amount": "0.01", "csrf_token": token}
    )
    assert r.headers["location"].startswith("/wallet?error=")
    r = await store.client.post(
        "/web/wallet/topup", data={"amount": "abc", "csrf_token": token}
    )
    assert r.headers["location"].startswith("/wallet?error=")


# --------------------------------------------------------------------------- #
# Login return path (?next=)
# --------------------------------------------------------------------------- #
from app.webshop.auth import safe_next_path  # noqa: E402


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/p/12", "/p/12"),
        ("/shop?cat=streaming&q=net", "/shop?cat=streaming&q=net"),
        ("/developer", "/developer"),
        ("", ""),
        (None, ""),
        ("p/12", ""),
        ("//evil.example/x", ""),
        ("/\\evil.example", ""),
        ("https://evil.example", ""),
        ("/ok\r\nSet-Cookie: x=1", ""),
        ("/" + "a" * 600, ""),
    ],
)
def test_safe_next_path(value, expected):
    assert safe_next_path(value) == expected


@pytest.mark.asyncio
async def test_login_returns_to_next_path(store):
    params = _telegram_params(4242)
    params["next"] = "/p/7"  # added by us to the auth URL, not signed
    r = await store.client.get("/auth/telegram", params=params)
    assert r.headers["location"] == "/p/7"
    assert SESSION_COOKIE in r.headers.get("set-cookie", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("evil", ["//evil.example", "https://evil.example", "/\\evil"])
async def test_login_never_redirects_off_site(store, evil):
    params = _telegram_params(4242)
    params["next"] = evil
    r = await store.client.get("/auth/telegram", params=params)
    assert r.headers["location"] == "/"


@pytest.mark.asyncio
async def test_failed_login_with_next_returns_there_with_flag(store):
    params = _telegram_params(4242)
    params["id"] = "1"  # tampered
    params["next"] = "/p/7"
    r = await store.client.get("/auth/telegram", params=params)
    assert r.headers["location"] == "/p/7?login=failed"
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_product_page_widget_returns_to_product(store):
    p = await store.product()
    r = await store.client.get(f"/p/{p.id}")
    assert f"/auth/telegram?next=%2Fp%2F{p.id}" in r.text


@pytest.mark.asyncio
async def test_developer_redirect_next_is_carried_into_widget(store):
    r = await store.client.get("/?next=/developer")
    assert "/auth/telegram?next=%2Fdeveloper" in r.text


# --------------------------------------------------------------------------- #
# /shop
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_shop_filters_by_query_and_category(store):
    await store.product("Netflix Premium", category="streaming", price="4.50")
    await store.product("Spotify Family", category="streaming", price="2.00")
    await store.product("Canva Pro", category="design", price="3.00")

    r = await store.client.get("/shop")
    assert r.status_code == 200
    for name in ("Netflix Premium", "Spotify Family", "Canva Pro"):
        assert name in r.text

    r = await store.client.get("/shop", params={"q": "netflix"})
    assert "Netflix Premium" in r.text
    assert "Spotify Family" not in r.text

    r = await store.client.get("/shop", params={"cat": "design"})
    assert "Canva Pro" in r.text
    assert "Netflix Premium" not in r.text

    # An unknown category is ignored rather than producing an empty page.
    r = await store.client.get("/shop", params={"cat": "nope"})
    assert "Netflix Premium" in r.text and "Canva Pro" in r.text


@pytest.mark.asyncio
async def test_shop_sorts_by_price(store):
    await store.product("Pricey", price="9.00")
    await store.product("Cheap", price="1.00")
    r = await store.client.get("/shop", params={"sort": "price-asc"})
    assert r.text.index("Cheap") < r.text.index("Pricey")
    r = await store.client.get("/shop", params={"sort": "price-desc"})
    assert r.text.index("Pricey") < r.text.index("Cheap")


@pytest.mark.asyncio
async def test_shop_empty_search_has_way_out(store):
    await store.product()
    r = await store.client.get("/shop", params={"q": "zzzz-no-match"})
    assert r.status_code == 200
    assert 'href="/shop"' in r.text


# --------------------------------------------------------------------------- #
# Storefront promotions
# --------------------------------------------------------------------------- #
from shared import storefront_promos  # noqa: E402
from shared.storefront_promos import PromoSlot  # noqa: E402


def test_promo_validation_rejects_bad_input():
    with pytest.raises(ValueError):
        storefront_promos.validate(PromoSlot(slot=4))
    with pytest.raises(ValueError):
        storefront_promos.validate(PromoSlot(slot=1, enabled=True, title="", target="1"))
    with pytest.raises(ValueError):
        storefront_promos.validate(PromoSlot(slot=1, enabled=True, title="T", target=""))
    with pytest.raises(ValueError):
        storefront_promos.validate(PromoSlot(slot=1, target_kind="product", target="abc"))
    with pytest.raises(ValueError):
        storefront_promos.validate(PromoSlot(slot=1, tone="neon"))
    with pytest.raises(ValueError):
        storefront_promos.validate(PromoSlot(slot=1, emoji_slot="not-a-slot"))
    long = storefront_promos.validate(
        PromoSlot(slot=1, enabled=True, title="x" * 200, target="1")
    )
    assert len(long.title) == storefront_promos.MAX_TITLE


@pytest.mark.asyncio
async def test_promos_roundtrip_and_resolve_against_live_catalog(store):
    p = await store.product("Netflix Premium", category="streaming", price="4.50")
    sold_out = await store.product("Gone", category="streaming", stock=0)
    async with store.maker() as s:
        await storefront_promos.save_slot(s, PromoSlot(
            slot=1, enabled=True, title="Netflix, sorted", target_kind="product",
            target=str(p.id), tone="brand", emoji_slot="premium",
        ))
        await storefront_promos.save_slot(s, PromoSlot(
            slot=2, enabled=True, title="All streaming", target_kind="category",
            target="streaming", tone="deep",
        ))
        await storefront_promos.save_slot(s, PromoSlot(
            slot=3, enabled=True, title="Sold out promo", target_kind="product",
            target=str(sold_out.id),
        ))
        slots = await storefront_promos.load_slots(s)
        overviews = [o for o in await services.list_product_overviews(s) if o.product.is_active]

    assert [x.title for x in slots] == ["Netflix, sorted", "All streaming", "Sold out promo"]
    resolved = storefront_promos.resolve(slots, overviews)
    assert [r.slot for r in resolved] == [1, 2]  # sold-out target is hidden
    assert resolved[0].href == f"/p/{p.id}"
    assert resolved[0].price == Decimal("4.50")
    assert resolved[1].href == "/shop?cat=streaming"
    assert resolved[1].meta == "2 products"

    r = await store.client.get("/")
    assert "Netflix, sorted" in r.text
    assert "All streaming" in r.text
    assert "Sold out promo" not in r.text


@pytest.mark.asyncio
async def test_disabled_or_cleared_promo_does_not_render(store):
    p = await store.product()
    async with store.maker() as s:
        await storefront_promos.save_slot(s, PromoSlot(
            slot=1, enabled=False, title="Hidden promo", target=str(p.id),
        ))
        await storefront_promos.save_slot(s, PromoSlot(
            slot=2, enabled=True, title="Cleared promo", target=str(p.id),
        ))
        await storefront_promos.clear_slot(s, 2)
    r = await store.client.get("/")
    assert "Hidden promo" not in r.text
    assert "Cleared promo" not in r.text


@pytest.mark.asyncio
async def test_corrupt_promo_record_reads_as_empty(store):
    from shared.models import AppSetting

    async with store.maker() as s:
        s.add(AppSetting(key="storefront_promo:1", value="{not json"))
        await s.commit()
    async with store.maker() as s:
        slots = await storefront_promos.load_slots(s)
    assert slots[0] == PromoSlot(slot=1)
    r = await store.client.get("/")
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Animated emoji route
# --------------------------------------------------------------------------- #
from shared import bot_appearance  # noqa: E402

EMOJI_ID = "5368324170671202286"


@pytest.mark.asyncio
async def test_emoji_route_404_for_unknown_or_unconfigured_slot(store):
    assert (await store.client.get("/web/emoji/not-a-slot")).status_code == 404
    assert (await store.client.get("/web/emoji/success")).status_code == 404


@pytest.mark.asyncio
async def test_emoji_route_serves_and_caches_configured_sticker(store, monkeypatch):
    async with store.maker() as s:
        await bot_appearance.save_slot(s, "success", EMOJI_ID, True, "✅")
    calls = []

    async def fake_fetch(custom_id):
        calls.append(custom_id)
        return b'{"v":"5.5.2","layers":[]}', "json"

    monkeypatch.setattr(web_emoji, "_fetch_from_telegram", fake_fetch)
    r = await store.client.get(f"/web/emoji/success?v={EMOJI_ID}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "max-age" in r.headers["cache-control"]
    r = await store.client.get(f"/web/emoji/success?v={EMOJI_ID}")
    assert r.status_code == 200
    assert calls == [EMOJI_ID]  # second hit came from the disk cache
    assert (web_emoji.CACHE_DIR / f"{EMOJI_ID}.json").is_file()


@pytest.mark.asyncio
async def test_emoji_route_respects_global_and_slot_switches(store):
    async with store.maker() as s:
        await bot_appearance.save_slot(s, "success", EMOJI_ID, False, "✅")
    assert (await store.client.get("/web/emoji/success")).status_code == 404

    web_emoji.appearance_cache.expires = 0
    async with store.maker() as s:
        await bot_appearance.save_slot(s, "success", EMOJI_ID, True, "✅")
        await bot_appearance.set_global_enabled(s, False)
    assert (await store.client.get("/web/emoji/success")).status_code == 404


@pytest.mark.asyncio
async def test_emoji_fetch_failure_is_404_and_not_retried_immediately(store, monkeypatch):
    async with store.maker() as s:
        await bot_appearance.save_slot(s, "success", EMOJI_ID, True, "✅")
    calls = []

    async def failing_fetch(custom_id):
        calls.append(custom_id)
        raise RuntimeError("telegram down")

    monkeypatch.setattr(web_emoji, "_fetch_from_telegram", failing_fetch)
    assert (await store.client.get("/web/emoji/success")).status_code == 404
    assert (await store.client.get("/web/emoji/success")).status_code == 404
    assert calls == [EMOJI_ID]


@pytest.mark.asyncio
async def test_pages_mark_configured_emoji_for_animation(store):
    async with store.maker() as s:
        await bot_appearance.save_slot(s, "orders", EMOJI_ID, True, "\U0001F4CB")
    await store.user()
    store.sign_in()
    r = await store.client.get("/my/orders")  # empty state uses the slot
    assert f"/web/emoji/orders?v={EMOJI_ID}" in r.text
    assert "\U0001F4CB" in r.text  # Unicode fallback is always in the HTML


# --------------------------------------------------------------------------- #
# Quantity limits shown match what the server enforces
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pandora_product_stepper_uses_supplier_min_and_max(store):
    from shared.models import SupplierProduct

    await store.user()
    p = await store.product("Supplier Item", stock=0)
    async with store.maker() as s:
        s.add(SupplierProduct(
            product_id=p.id, supplier_product_id="pd-1", supplier_name="Supplier Item",
            supplier_unit_price=Decimal("1.00"), available_stock=500,
            minimum_quantity=5, maximum_quantity=20,
        ))
        await s.commit()
    store.sign_in()
    r = await store.client.get(f"/p/{p.id}")
    m = re.search(r'<input[^>]*name="quantity"[^>]*>', r.text, flags=re.S)
    assert m, "quantity input missing"
    tag = m.group(0)
    assert 'min="5"' in tag and 'max="20"' in tag and 'value="5"' in tag


# --------------------------------------------------------------------------- #
# Orders list shows what was bought
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_my_orders_lists_product_names_and_quantities(store):
    owner = await store.user()
    p = await store.product("Spotify Family", stock=3)
    await store.order(owner.id, p.id, 2)
    store.sign_in()
    r = await store.client.get("/my/orders")
    assert "Spotify Family" in r.text
    assert re.search(r"(×|&times;)\s*2", r.text)


# --------------------------------------------------------------------------- #
# Admin promotion editor handlers
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_admin_promo_save_validate_guard_and_clear(store, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.web.admin.admin as panel
    from app.web.admin.admin import AdminState
    from shared import audit

    p = await store.product("Netflix Premium")
    monkeypatch.setattr(panel, "AsyncSessionLocal", store.maker)
    monkeypatch.setattr(audit, "log_action", AsyncMock())
    state = SimpleNamespace(
        authed=True, promo_slot="2", promo_enabled=True,
        promo_title="Netflix, sorted", promo_subtitle="Shared plans",
        promo_target_kind="product", promo_target_product=f"{p.id} — Netflix Premium",
        promo_target_category="", promo_cta="", promo_tone="deep",
        promo_emoji="premium", promo_message="",
        touch=lambda: None, load_promos=AsyncMock(),
    )
    await AdminState.save_promo.fn(state)
    async with store.maker() as s:
        slot = (await storefront_promos.load_slots(s))[1]
    assert slot.enabled and slot.title == "Netflix, sorted"
    assert slot.target == str(p.id) and slot.emoji_slot == "premium"
    assert "Saved" in state.promo_message

    state.promo_title = ""  # enabled promo without a title is refused
    await AdminState.save_promo.fn(state)
    assert state.promo_message.startswith("⚠")
    async with store.maker() as s:
        assert (await storefront_promos.load_slots(s))[1].title == "Netflix, sorted"

    state.authed = False
    state.promo_title = "Should not save"
    await AdminState.save_promo.fn(state)
    await AdminState.clear_promo.fn(state)
    async with store.maker() as s:
        assert (await storefront_promos.load_slots(s))[1].title == "Netflix, sorted"

    state.authed = True
    await AdminState.clear_promo.fn(state)
    async with store.maker() as s:
        assert (await storefront_promos.load_slots(s))[1] == PromoSlot(slot=2)


# --------------------------------------------------------------------------- #
# Self-hosted assets and accurate legal copy
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, media",
    [
        ("/web/static/vendor/lottie_light.min.js?v=5.13.0", "javascript"),
        ("/web/static/fonts/Manrope-Variable.ttf?v=b31870a", "font"),
        ("/web/static/vendor/lottie-web.LICENSE.md", ""),
        ("/web/static/fonts/Manrope-OFL.txt", "text/plain"),
    ],
)
async def test_vendored_assets_are_served_locally(store, path, media):
    r = await store.client.get(path)
    assert r.status_code == 200, path
    if media:
        assert media in r.headers["content-type"], r.headers["content-type"]


@pytest.mark.asyncio
async def test_pages_load_no_external_fonts_or_scripts(store):
    p = await store.product()
    for path in ("/", "/shop", f"/p/{p.id}", "/legal/cookies"):
        html = (await store.client.get(path)).text
        assert "/web/static/fonts/Manrope-Variable.ttf" in html  # preload
        for host in ("fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.", "jsdelivr", "unpkg.com"):
            assert host not in html, (path, host)
        # The only third-party script host is Telegram's login widget,
        # injected by shop.js; no <script src> points anywhere else.
        for src in re.findall(r'<script[^>]+src="([^"]+)"', html):
            assert src.startswith("/web/static/"), src
    css = (await store.client.get("/web/static/shop.css")).text
    assert 'url("/web/static/fonts/Manrope-Variable.ttf' in css
    assert "Noto Sans Khmer" in css  # Khmer fallback kept in the stack


@pytest.mark.asyncio
async def test_legal_pages_do_not_claim_a_theme_preference(store):
    cookies = " ".join((await store.client.get("/legal/cookies")).text.split())
    privacy = " ".join((await store.client.get("/legal/privacy")).text.split())
    assert "light/dark theme" not in privacy
    assert "Remembers whether you chose the light or dark" not in cookies
    assert "returns the site to following your system" not in cookies
    assert "no longer reads or writes it" in cookies
