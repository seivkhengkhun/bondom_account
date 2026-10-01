"""One renderer and transport fallback for every Telegram interface."""

import asyncio
import html
import re
import time
from contextvars import ContextVar

from aiogram import BaseMiddleware, Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, KeyboardButton

from shared import bot_appearance as config
from shared.database import AsyncSessionLocal

current: ContextVar[config.Appearance] = ContextVar("bot_appearance", default=config.Appearance())


def emoji(key: str, *, supported: bool = True) -> str:
    appearance = current.get()
    slot = appearance.slots.get(key)
    if slot is None:
        return ""
    fallback = html.escape(slot.fallback_emoji)
    if supported and appearance.enabled and slot.enabled and slot.custom_emoji_id:
        return f'<tg-emoji emoji-id="{slot.custom_emoji_id}">{fallback}</tg-emoji>'
    return fallback


def _button_fields(cls, key: str, text: str, style: str | None) -> dict:
    appearance = current.get()
    slot = appearance.slots[key]
    fields = cls.model_fields
    custom = (appearance.enabled and slot.enabled and slot.custom_emoji_id
              and "icon_custom_emoji_id" in fields)
    result = {"text": text if custom else f"{slot.fallback_emoji} {text}"}
    if custom:
        result["icon_custom_emoji_id"] = slot.custom_emoji_id
    if style and "style" in fields:
        result["style"] = style
    return result


def inline_button(key: str, text: str, *, style: str | None = None, **kwargs):
    return InlineKeyboardButton(**_button_fields(InlineKeyboardButton, key, text, style), **kwargs)


def reply_button(key: str, text: str):
    # Old Unicode labels remain accepted by the handlers. With a custom icon,
    # display only the action text to avoid two icons on the same button.
    fields = _button_fields(KeyboardButton, key, text, None)
    fields["text"] = text.split(" ", 1)[-1] if fields.get("icon_custom_emoji_id") else text
    return KeyboardButton(**fields)


class AppearanceCache:
    def __init__(self):
        self.value = config.Appearance()
        self.expires = 0.0
        self.lock = asyncio.Lock()

    async def get(self):
        if time.monotonic() < self.expires:
            return self.value
        async with self.lock:
            if time.monotonic() >= self.expires:
                try:
                    async with AsyncSessionLocal() as session:
                        self.value = await config.load_appearance(session)
                except Exception:
                    # A config/database failure must not prevent a Unicode message.
                    self.value = config.Appearance(enabled=False)
                    config.warn_once("config_load_failed")
                self.expires = time.monotonic() + 5
        return self.value


cache = AppearanceCache()


class AppearanceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        token = current.set(await cache.get())
        try:
            return await handler(event, data)
        finally:
            current.reset(token)


def plain_method(method):
    """Copy an outgoing method; remove only appearance enhancements."""
    changes = {}
    for name in ("text", "caption"):
        value = getattr(method, name, None)
        if isinstance(value, str) and "<tg-emoji" in value:
            changes[name] = re.sub(r'<tg-emoji\s+emoji-id="[0-9]+">(.*?)</tg-emoji>',
                                   r"\1", value, flags=re.DOTALL)
    for name in ("entities", "caption_entities"):
        entities = getattr(method, name, None)
        if entities and any(e.type == "custom_emoji" for e in entities):
            changes[name] = [e for e in entities if e.type != "custom_emoji"]
    markup = getattr(method, "reply_markup", None)
    if markup is not None:
        field = "inline_keyboard" if hasattr(markup, "inline_keyboard") else "keyboard"
        rows = getattr(markup, field, None)
        if rows:
            clean_rows = []
            changed = False
            for row in rows:
                clean_row = []
                for button in row:
                    custom_id = getattr(button, "icon_custom_emoji_id", None)
                    style = getattr(button, "style", None)
                    updates = {}
                    if custom_id:
                        updates["icon_custom_emoji_id"] = None
                        if field == "inline_keyboard" or not any(button.text.startswith(value) for value in config.DEFAULTS.values()):
                            fallback = next((s.fallback_emoji for s in current.get().slots.values()
                                             if s.custom_emoji_id == custom_id), "•")
                            updates["text"] = f"{fallback} {button.text}"
                    if style:
                        updates["style"] = None
                    changed |= bool(updates)
                    clean_row.append(button.model_copy(update=updates) if updates else button)
                clean_rows.append(clean_row)
            if changed:
                changes["reply_markup"] = markup.model_copy(update={field: clean_rows})
    return method.model_copy(update=changes) if changes else None


class EmojiFallbackMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            reason = exc.message.lower()
            if not any(word in reason for word in (
                "emoji", "icon_custom", "button style", "button_style", "field \"style\"",
                "unsupported start tag \"tg-emoji\"", "can't parse entities",
            )):
                raise
            plain = plain_method(method)
            if plain is None:
                raise
            config.warn_once("telegram_rejected_appearance")
            # One retry only. Other errors propagate, never replay business logic.
            return await make_request(bot, plain)


def create_bot(token: str) -> Bot:
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(EmojiFallbackMiddleware())
    return bot


async def validate_custom_emoji(bot: Bot, custom_id: str) -> str | None:
    """Telegram validates the ID and supplies the exact Unicode alt it requires."""
    custom_id = config.validate_id(custom_id)
    if not custom_id:
        return None
    stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=[custom_id])
    if len(stickers) != 1 or stickers[0].type != "custom_emoji" or not stickers[0].emoji:
        raise ValueError("Telegram did not recognize this custom emoji ID.")
    return stickers[0].emoji


def extracted_ids(message) -> list[str]:
    ids = [e.custom_emoji_id for e in (message.entities or message.caption_entities or [])
           if e.type == "custom_emoji" and e.custom_emoji_id]
    if message.sticker and message.sticker.custom_emoji_id:
        ids.append(message.sticker.custom_emoji_id)
    return list(dict.fromkeys(ids))
