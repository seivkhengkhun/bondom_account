"""Isolated database and aiogram transport tests; no live payments/messages."""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree

import pytest
import pytest_asyncio
from aiogram.fsm.storage.base import StorageKey
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage, SendPhoto, EditMessageText
from aiogram.types import (InlineKeyboardMarkup, KeyboardButton, Message, MessageEntity,
                           ReplyKeyboardMarkup, Update, User as TelegramUser)
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared import bot_appearance as config, services
from shared.database import Base
from shared.models import AppSetting, Inventory, Product, OrderStatus
from shared.schemas import ProductCreate
from app.bot import appearance, handlers, admin_appearance, runner

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, url, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__api_method__ == "getCustomEmojiStickers":
            if method.custom_emoji_ids == ["999"]:
                return []
            return [SimpleNamespace(type="custom_emoji", emoji="💰")]
        if method.__api_method__ == "getMe":
            return TelegramUser(id=123456789, is_bot=True, first_name="Bondom", username="test_bot")
        if method.__api_method__ == "answerCallbackQuery":
            return True
        return Message(message_id=100, date=0,
                       chat={"id": getattr(method, "chat_id", 111), "type": "private"},
                       text=getattr(method, "text", None)).as_(bot)


# Routers are attached once, just as they are in a running bot process.
dispatcher = runner.build_dispatcher()


@pytest_asyncio.fixture
async def store(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'appearance.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(handlers, "AsyncSessionLocal", maker)
    monkeypatch.setattr(appearance, "AsyncSessionLocal", maker)
    monkeypatch.setattr(admin_appearance, "AsyncSessionLocal", maker)
    monkeypatch.setattr(appearance.cache, "expires", 0)
    monkeypatch.setattr(appearance.cache, "lock", asyncio.Lock())
    monkeypatch.setattr(appearance.cache, "value", config.Appearance())
    monkeypatch.setattr(handlers.settings, "telegram_admin_ids", [111])
    monkeypatch.setattr(handlers.settings, "support_username", "bondom_support")
    monkeypatch.setattr(handlers.settings, "sms_enabled", False)
    bot = appearance.create_bot(TOKEN)
    transport = RecordingSession()
    transport.middleware(appearance.EmojiFallbackMiddleware())
    await bot.session.close()
    bot.session = transport
    async with maker() as session:
        user = await services.get_or_create_user(session, 111, "Buyer <&>")
        product = await services.create_product(session, ProductCreate(
            name="Gemini <Pro> & Co", price=Decimal("3.75"), category="AI & tools", warranty_days=7
        ))
        session.add(Inventory(product_id=product.id, data="login<&>:secret"))
        await session.commit()
    async def feed(text=None, callback=None, telegram_id=111, entities=None):
        message = {"message_id": 1, "date": 0, "chat": {"id": telegram_id, "type": "private"},
                   "from": {"id": telegram_id, "is_bot": False, "first_name": "Buyer", "username": "Buyer<&>"},
                   "text": text or "menu", "entities": entities or []}
        if callback:
            data = {"update_id": 1, "callback_query": {"id": "test", "from": message["from"],
                                                      "chat_instance": "test", "data": callback, "message": message}}
        else:
            data = {"update_id": 1, "message": message}
        await dispatcher.feed_update(bot, Update.model_validate(data))
        return transport.calls
    yield SimpleNamespace(maker=maker, bot=bot, transport=transport, feed=feed, product=product, user=user)
    await dispatcher.storage.set_state(StorageKey(bot.id, 111, 111), None)
    await dispatcher.storage.set_data(StorageKey(bot.id, 111, 111), {})
    for task in list(handlers._background_tasks):
        task.cancel()
    await bot.session.close()
    await engine.dispose()


def check_html(text):
    ElementTree.fromstring(f"<root>{text}</root>")


@pytest.mark.asyncio
async def test_settings_persist_and_keep_existing_data(store):
    async with store.maker() as session:
        session.add(AppSetting(key="unrelated", value="keep"))
        await session.commit()
        await config.save_slot(session, "money", "12345", True, "💰")
        await config.set_global_enabled(session, False)
    # New engine simulates a restart, not just a cached object.
    fresh_engine = create_async_engine(str(store.maker.kw['bind'].url))
    fresh_maker = async_sessionmaker(fresh_engine, expire_on_commit=False)
    try:
        async with fresh_maker() as session:
            loaded = await config.load_appearance(session)
            assert not loaded.enabled
            assert loaded.slots['money'].custom_emoji_id == '12345'
            assert loaded.slots['money'].updated_at
            assert (await session.get(AppSetting, 'unrelated')).value == 'keep'
            assert (await session.get(Product, store.product.id)).name == store.product.name
    finally:
        await fresh_engine.dispose()


@pytest.mark.asyncio
async def test_defaults_bad_config_reset_and_individual_disable(store):
    async with store.maker() as session:
        loaded = await config.load_appearance(session)
        assert len(loaded.slots) == 17
        session.add(AppSetting(key=config.PREFIX+'home', value='broken json'))
        await session.commit()
        await config.save_slot(session, 'money', '12345', False)
        loaded = await config.load_appearance(session)
        assert loaded.slots['home'].fallback_emoji == '🏠'
        assert not loaded.slots['money'].enabled
        await config.reset_slot(session, 'money')
        loaded = await config.load_appearance(session)
        assert loaded.slots['money'].custom_emoji_id == ''


@pytest.mark.parametrize('value', ['abc', '-123', '1" onclick="x', '0', '1'*21])
def test_invalid_ids_rejected(value):
    with pytest.raises(ValueError):
        config.validate_id(value)


def test_render_modes_and_old_library():
    slots = dict(config.Appearance().slots)
    slots['money'] = config.EmojiSlot('money', '12345', '💰')
    token = appearance.current.set(config.Appearance(True, slots))
    try:
        assert appearance.emoji('money') == '<tg-emoji emoji-id="12345">💰</tg-emoji>'
        assert appearance.emoji('money', supported=False) == '💰'
        assert appearance.emoji('missing') == ''
        button = appearance.inline_button('money', 'Pay', callback_data='buy:1', style='primary')
        assert button.icon_custom_emoji_id == '12345'
        assert button.callback_data == 'buy:1'
        assert appearance._button_fields(SimpleNamespace(model_fields={'text': None}), 'money', 'Pay', 'primary') == {'text': '💰 Pay'}
        appearance.current.set(config.Appearance(False, slots))
        assert appearance.emoji('money') == '💰'
        assert appearance.inline_button('money', 'Pay', callback_data='buy:1').icon_custom_emoji_id is None
        slots['money'] = config.EmojiSlot('money', '12345', '💰', False)
        appearance.current.set(config.Appearance(True, slots))
        assert appearance.emoji('money') == '💰'
    finally:
        appearance.current.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('method_type', [SendMessage, SendPhoto, EditMessageText])
async def test_api_rejection_retries_only_presentation(method_type):
    slots = dict(config.Appearance().slots)
    slots['money'] = config.EmojiSlot('money', '12345', '💰')
    token = appearance.current.set(config.Appearance(True, slots))
    calls = []
    async def request(bot, method):
        calls.append(method)
        if len(calls) == 1:
            raise TelegramBadRequest(method=method, message='CUSTOM_EMOJI_INVALID')
        return True
    try:
        kwargs = dict(chat_id=111, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            appearance.inline_button('money', 'Pay', callback_data='chk:1', style='primary')]]))
        text = f"{appearance.emoji('money')} <b>Pay</b>"
        if method_type is SendPhoto:
            kwargs.update(photo='file-id', caption=text)
        else:
            kwargs.update(text=text)
        if method_type is EditMessageText:
            kwargs['message_id'] = 1
        await appearance.EmojiFallbackMiddleware()(request, None, method_type(**kwargs))
        assert len(calls) == 2
        button = calls[1].reply_markup.inline_keyboard[0][0]
        assert button.text == '💰 Pay' and button.callback_data == 'chk:1'
        assert button.icon_custom_emoji_id is None and button.style is None
        assert '<tg-emoji' not in (getattr(calls[1], 'text', '') or getattr(calls[1], 'caption', ''))
    finally:
        appearance.current.reset(token)


@pytest.mark.asyncio
async def test_unrelated_badrequest_is_never_retried():
    request = AsyncMock(side_effect=TelegramBadRequest(method=SendMessage(chat_id=1, text='x'), message='chat not found'))
    with pytest.raises(TelegramBadRequest):
        await appearance.EmojiFallbackMiddleware()(request, None, SendMessage(chat_id=1, text='x'))
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_config_failure_is_unicode(monkeypatch):
    @asynccontextmanager
    async def broken():
        raise RuntimeError('database unavailable')
        yield
    monkeypatch.setattr(appearance, 'AsyncSessionLocal', broken)
    loaded = await appearance.AppearanceCache().get()
    assert not loaded.enabled and loaded.slots['money'].fallback_emoji == '💰'


@pytest.mark.asyncio
async def test_main_catalog_details_balance_orders_account_support(store):
    async with store.maker() as session:
        await config.save_slot(session, 'home', '12345', True)
    await store.feed('/start')
    menu = store.transport.calls[-1]
    assert '<tg-emoji' in menu.text
    assert menu.reply_markup.keyboard[0][0].text == handlers.BTN_BROWSE
    await store.feed(handlers.BTN_BROWSE)
    catalog = store.transport.calls[-1]
    assert catalog.reply_markup.inline_keyboard[0][0].callback_data == f'pview:{store.product.id}:0:0'
    await store.feed(callback=f'pview:{store.product.id}:0:0')
    detail = next(c for c in reversed(store.transport.calls) if c.__api_method__ == 'editMessageText')
    assert '&lt;Pro&gt; &amp; Co' in detail.text
    assert [b.callback_data for b in detail.reply_markup.inline_keyboard[0]] == [f'buy:{store.product.id}', f'wb1:{store.product.id}']
    for label in [handlers.BTN_BALANCE, handlers.BTN_ORDERS, handlers.BTN_ACCOUNT, handlers.BTN_SUPPORT]:
        await store.feed(label)
    for call in store.transport.calls:
        text = getattr(call, 'text', None) or getattr(call, 'caption', None)
        if text:
            check_html(text)
    assert any('Your Account' in (getattr(c, 'text', '') or '') for c in store.transport.calls)
    assert any('Support' in (getattr(c, 'text', '') or '') for c in store.transport.calls)


@pytest.mark.asyncio
async def test_reply_custom_icon_labels_and_old_keyboard_compatibility(store):
    async with store.maker() as session:
        for key in ['shop', 'money', 'balance', 'orders', 'account', 'support']:
            await config.save_slot(session, key, '12345', True)
    await store.feed('/start')
    menu = store.transport.calls[-1]
    assert menu.reply_markup.keyboard[0][0].text == 'Browse Products'
    assert menu.reply_markup.keyboard[0][0].icon_custom_emoji_id == '12345'
    for label in ['Browse Products', handlers.BTN_BROWSE, 'My Balance', handlers.BTN_BALANCE,
                  'My Orders', handlers.BTN_ORDERS, 'Account', 'Support']:
        before = len(store.transport.calls)
        await store.feed(label)
        assert len(store.transport.calls) > before
    async with store.maker() as session:
        loaded = await config.load_appearance(session)
    token = appearance.current.set(loaded)
    try:
        plain = appearance.plain_method(menu)
    finally:
        appearance.current.reset(token)
    assert plain.reply_markup.keyboard[0][0].icon_custom_emoji_id is None
    assert plain.reply_markup.keyboard[0][0].text.startswith('🛍')


@pytest.mark.asyncio
async def test_catalog_navigation_callbacks_and_no_duplicate_filters(store):
    async with store.maker() as session:
        await services.create_product(session, ProductCreate(
            name='Second', price=Decimal('1'), category='Other', warranty_days=0))
    await store.feed(handlers.BTN_BROWSE)
    assert store.transport.calls[-1].reply_markup.inline_keyboard[0][0].callback_data == 'pcat:0:0'
    for callback in ['pcats', 'pcat:0:0', f'pview:{store.product.id}:0:0', 'pcat:0:0']:
        before = len(store.transport.calls)
        await store.feed(callback=callback)
        assert len(store.transport.calls) > before
    # Each callback handler is registered once; no duplicate callback functions.
    registered = [h.callback for h in handlers.router.callback_query.handlers]
    assert len(registered) == len(set(registered))


@pytest.mark.asyncio
async def test_appearance_save_database_failure_is_reported(store, monkeypatch):
    monkeypatch.setattr(admin_appearance.config, 'set_global_enabled',
                        AsyncMock(side_effect=OperationalError('update', {}, RuntimeError('offline'))))
    await store.feed(callback='appearance:global')
    assert 'could not be saved' in store.transport.calls[-1].text


@pytest.mark.asyncio
async def test_repeated_emoji_error_retries_once_and_reply_text_stays_usable():
    method = SendMessage(chat_id=111, text='<tg-emoji emoji-id="12345">💰</tg-emoji>',
                         reply_markup=ReplyKeyboardMarkup(keyboard=[[
                             KeyboardButton(text=handlers.BTN_BALANCE, icon_custom_emoji_id='12345')]]))
    request = AsyncMock(side_effect=TelegramBadRequest(method=method, message='CUSTOM_EMOJI_INVALID'))
    with pytest.raises(TelegramBadRequest):
        await appearance.EmojiFallbackMiddleware()(request, None, method)
    assert request.await_count == 2
    assert request.await_args.args[1].reply_markup.keyboard[0][0].text == handlers.BTN_BALANCE


@pytest.mark.asyncio
async def test_wallet_insufficient_success_failed_and_delivery(store):
    await store.feed(callback=f'wb1:{store.product.id}')
    assert any('Insufficient Balance' in (getattr(c, 'text', '') or '') for c in store.transport.calls)
    async with store.maker() as session:
        await services.add_user_balance(session, store.user.id, Decimal('10'))
    await store.feed(callback=f'wb1:{store.product.id}')
    success = next(c for c in store.transport.calls if 'PURCHASE SUCCESSFUL' in (getattr(c, 'text', '') or ''))
    assert 'login&lt;&amp;&gt;:secret' in success.text
    check_html(success.text)
    assert [b.callback_data for b in success.reply_markup.inline_keyboard[0]] == ['ui:account', 'ui:products']
    assert any(c.__api_method__ == 'sendDocument' for c in store.transport.calls)
    async with store.maker() as session:
        orders = await services.list_user_orders(session, store.user.id)
        assert orders[0].status is OrderStatus.DELIVERED
        assert await services.get_user_balance(session, store.user.id) == Decimal('6.25')
    await store.feed(handlers.BTN_ORDERS)
    assert any('Your recent orders' in (getattr(c, 'text', '') or '') for c in store.transport.calls)
    await store.feed(callback=f'wb1:{store.product.id}')
    assert any('Purchase Failed' in (getattr(c, 'text', '') or '') for c in store.transport.calls)


@pytest.mark.asyncio
async def test_purchase_confirmation_and_add_balance(store):
    await store.feed(callback=f'buy:{store.product.id}')
    confirm = next(c for c in store.transport.calls if 'Purchase Confirmation' in (getattr(c, 'text', '') or ''))
    assert '&lt;Pro&gt;' in confirm.text
    check_html(confirm.text)
    assert confirm.reply_markup.inline_keyboard[0][0].callback_data == 'buy_cancel'
    await store.feed(callback='buy_cancel')
    await store.feed(handlers.BTN_TOPUP)
    assert 'Add Balance' in store.transport.calls[-1].text


@pytest.mark.asyncio
async def test_admin_edit_extract_validate_disable_reset_global(store):
    await store.feed('/admin')
    await store.feed(callback='appearance:menu')
    await store.feed(callback='appearance:list')
    assert len(store.transport.calls[-2].reply_markup.inline_keyboard) == 19
    await store.feed(callback='appearance:slot:money')
    await store.feed(callback='appearance:edit:money')
    await store.feed('999')
    assert 'could not be validated' in store.transport.calls[-1].text
    await store.feed('💰', entities=[{'type': 'custom_emoji', 'offset': 0, 'length': 2, 'custom_emoji_id': '12345'}])
    async with store.maker() as session:
        loaded = await config.load_appearance(session)
        assert loaded.slots['money'].custom_emoji_id == '12345'
    await store.feed(callback='appearance:preview:money')
    assert any('<tg-emoji emoji-id="12345">' in (getattr(c, 'text', '') or '') for c in store.transport.calls)
    await store.feed(callback='appearance:toggle:money')
    await store.feed(callback='appearance:global')
    async with store.maker() as session:
        loaded = await config.load_appearance(session)
        assert not loaded.enabled and not loaded.slots['money'].enabled
    await store.feed(callback='appearance:reset:money')
    async with store.maker() as session:
        assert (await config.load_appearance(session)).slots['money'].custom_emoji_id == ''
    before = len(store.transport.calls)
    await store.feed(callback='appearance:global', telegram_id=222)
    await store.feed('/admin', telegram_id=222)
    assert len(store.transport.calls) == before


def test_extraction_entities_captions_stickers_and_no_custom_emoji():
    message = SimpleNamespace(entities=[MessageEntity(type='custom_emoji', offset=0, length=2, custom_emoji_id='12345')],
                              caption_entities=None, sticker=None)
    assert appearance.extracted_ids(message) == ['12345']
    message.caption_entities, message.entities = message.entities, None
    assert appearance.extracted_ids(message) == ['12345']
    message.caption_entities = None
    message.sticker = SimpleNamespace(custom_emoji_id='54321')
    assert appearance.extracted_ids(message) == ['54321']
    message.sticker = None
    assert appearance.extracted_ids(message) == []


@pytest.mark.asyncio
async def test_bot_startup_entrypoint(store, monkeypatch):
    monkeypatch.setattr(runner.settings, 'bot_token', TOKEN)
    monkeypatch.setattr(runner, 'create_bot', lambda token: store.bot)
    monkeypatch.setattr(runner, 'build_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(__import__('shared.database', fromlist=['init_db']), 'init_db', AsyncMock())
    monkeypatch.setattr(dispatcher, 'start_polling', AsyncMock())
    await runner.run_bot()
    dispatcher.start_polling.assert_awaited_once_with(store.bot)
    assert store.transport.calls[-1].__api_method__ == 'deleteWebhook'


def test_web_admin_components_build():
    from app.web.admin.admin import appearance_tab, dashboard_view
    assert appearance_tab() is not None and dashboard_view() is not None


@pytest.mark.asyncio
async def test_real_polling_loop_starts_and_stops(store, monkeypatch):
    started = asyncio.Event()
    original_request = store.transport.make_request
    async def request(bot, method, timeout=None):
        if method.__api_method__ == 'getUpdates':
            started.set()
            await asyncio.sleep(0.02)
            return []
        return await original_request(bot, method, timeout)
    monkeypatch.setattr(store.transport, 'make_request', request)
    monkeypatch.setattr(runner.settings, 'bot_token', TOKEN)
    monkeypatch.setattr(runner, 'create_bot', lambda token: store.bot)
    monkeypatch.setattr(runner, 'build_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(__import__('shared.database', fromlist=['init_db']), 'init_db', AsyncMock())
    task = asyncio.create_task(runner.run_bot())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(dispatcher.stop_polling(), timeout=2)
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
    assert any(c.__api_method__ == 'getMe' for c in store.transport.calls)


@pytest.mark.asyncio
async def test_khqr_checkout_and_auto_delivery(store, monkeypatch):
    monkeypatch.setattr(handlers.settings, 'payment_dev_mode', True)
    monkeypatch.setattr(handlers.payment_service, 'render_khqr_card_png', lambda *args, **kwargs: b'test-image')
    # Prevent a live background poll; exercise auto-delivery explicitly below.
    monkeypatch.setattr(handlers, '_track_background_task', lambda task, label: task.cancel())
    handlers.payment_limits.reset('order', store.user.id)
    await store.feed(callback=f'buy:{store.product.id}')
    await store.feed('1')
    photo = next(c for c in store.transport.calls if c.__api_method__ == 'sendPhoto')
    check_html(photo.caption)
    async with store.maker() as session:
        order = (await services.list_user_orders(session, store.user.id))[0]
        payment = await handlers.payment_service.get_latest_payment(session, order.id)
        await handlers.payment_service.confirm_payment(session, payment.id)
    monkeypatch.setattr(handlers.payment_service, 'poll_payment_until_paid', AsyncMock(return_value=True))
    await handlers._watch_payment_and_auto_deliver(store.bot, 111, order.id, payment.id)
    assert any('PURCHASE SUCCESSFUL' in (getattr(c, 'text', '') or '') for c in store.transport.calls)
    async with store.maker() as session:
        assert (await services.get_order_with_items(session, order.id)).status is OrderStatus.DELIVERED
    handlers.payment_limits.reset('order', store.user.id)


@pytest.mark.asyncio
async def test_admin_web_save_toggle_reset_and_auth(store, monkeypatch):
    from app.web.admin.admin import AdminState
    import app.web.admin.admin as panel
    monkeypatch.setattr(panel, 'AsyncSessionLocal', store.maker)
    monkeypatch.setattr(panel, 'create_bot', lambda token: store.bot)
    monkeypatch.setattr(panel.settings, 'bot_token', TOKEN)
    state = SimpleNamespace(authed=True, emoji_id='12345', emoji_selected='money',
                            emoji_enabled=True, emoji_fallback='💰', emoji_message='',
                            touch=lambda: None, load_appearance=AsyncMock())
    await AdminState.save_emoji.fn(state)
    async with store.maker() as session:
        assert (await config.load_appearance(session)).slots['money'].custom_emoji_id == '12345'
    await AdminState.toggle_animated_emoji.fn(state, False)
    async with store.maker() as session:
        assert not (await config.load_appearance(session)).enabled
    await AdminState.reset_emoji.fn(state)
    async with store.maker() as session:
        assert (await config.load_appearance(session)).slots['money'].custom_emoji_id == ''
    state.authed = False
    await AdminState.save_emoji.fn(state)
    async with store.maker() as session:
        assert (await config.load_appearance(session)).slots['money'].custom_emoji_id == ''
