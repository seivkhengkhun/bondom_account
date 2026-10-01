"""Durable bot appearance settings; defaults require no schema migration."""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from shared.models import AppSetting
from shared.services import transaction_scope

logger = logging.getLogger(__name__)
PREFIX = "bot_emoji:"
GLOBAL_KEY = "bot_animated_emoji_enabled"
DEFAULTS = {
    "home": "🏠", "shop": "🛍", "product": "📦", "cart": "🛒",
    "money": "💰", "balance": "💳", "orders": "📋", "success": "✅",
    "warning": "⚠️", "delivery": "⚡", "account": "👤", "support": "💬",
    "fire": "🔥", "gift": "🎁", "premium": "💎", "back": "◀️", "refresh": "🔄",
}
_last_warning: dict[str, float] = {}


def warn_once(code: str) -> None:
    """Log a category at most once per minute, never message/customer data."""
    now = time.monotonic()
    if now - _last_warning.get(code, -1000) >= 60:
        logger.warning("Bot appearance fallback: %s", code)
        _last_warning[code] = now


def validate_id(value: str) -> str:
    value = value.strip()
    if value and not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError("Custom emoji ID must be a positive numeric ID (up to 20 digits).")
    return value


@dataclass(frozen=True)
class EmojiSlot:
    key: str
    custom_emoji_id: str = ""
    fallback_emoji: str = ""
    enabled: bool = True
    updated_at: str = ""


@dataclass(frozen=True)
class Appearance:
    enabled: bool = True
    slots: dict[str, EmojiSlot] = field(default_factory=lambda: {
        key: EmojiSlot(key, fallback_emoji=value) for key, value in DEFAULTS.items()
    })


async def load_appearance(session) -> Appearance:
    async with transaction_scope(session):
        records = (await session.scalars(select(AppSetting).where(
            AppSetting.key.startswith(PREFIX) | (AppSetting.key == GLOBAL_KEY)
        ))).all()
        rows = [(row.key, row.value) for row in records]
    defaults = Appearance()
    slots = dict(defaults.slots)
    enabled = True
    for setting_key, setting_value in rows:
        if setting_key == GLOBAL_KEY:
            enabled = setting_value == "true"
            continue
        key = setting_key.removeprefix(PREFIX)
        if key not in DEFAULTS:
            continue
        try:
            data = json.loads(setting_value)
            custom_id = validate_id(data.get("custom_emoji_id", ""))
            fallback = data.get("fallback_emoji") or DEFAULTS[key]
            if not isinstance(fallback, str) or len(fallback) > 32:
                raise ValueError("Invalid fallback")
            if type(data.get("enabled", True)) is not bool:
                raise ValueError("Invalid enabled flag")
            slots[key] = EmojiSlot(key, custom_id, fallback,
                                  data.get("enabled", True), data.get("updated_at", ""))
        except (ValueError, TypeError, AttributeError):
            warn_once("invalid_slot_config")
    return Appearance(enabled, slots)


async def save_slot(session, key: str, custom_emoji_id: str, enabled: bool,
                    fallback_emoji: str | None = None) -> EmojiSlot:
    if key not in DEFAULTS:
        raise ValueError("Unknown emoji slot")
    custom_id = validate_id(custom_emoji_id)
    fallback = fallback_emoji or DEFAULTS[key]
    if not isinstance(fallback, str) or len(fallback) > 32:
        raise ValueError("Invalid fallback emoji")
    slot = EmojiSlot(key, custom_id, fallback, bool(enabled),
                     datetime.now(timezone.utc).isoformat())
    async with transaction_scope(session):
        row = await session.get(AppSetting, PREFIX + key)
        value = json.dumps(asdict(slot), ensure_ascii=False)
        if row is None:
            session.add(AppSetting(key=PREFIX + key, value=value))
        else:
            row.value = value
    return slot


async def reset_slot(session, key: str) -> EmojiSlot:
    return await save_slot(session, key, "", True, DEFAULTS[key])


async def set_global_enabled(session, enabled: bool) -> None:
    async with transaction_scope(session):
        row = await session.get(AppSetting, GLOBAL_KEY)
        value = "true" if enabled else "false"
        if row is None:
            session.add(AppSetting(key=GLOBAL_KEY, value=value))
        else:
            row.value = value
