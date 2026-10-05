"""Animated Telegram emoji for the website, from the bot's appearance slots.

The bot's 17 appearance slots (``shared/bot_appearance.py``) hold Telegram
custom-emoji IDs. Browsers cannot render those IDs, so this route resolves
a slot's ID through the Bot API, downloads the sticker file once, caches it
on disk and serves it from our own origin. The bot token never reaches the
browser, and only configured, enabled slots are ever looked up, so a visitor
cannot make the server fetch arbitrary IDs.

Served formats, chosen by what Telegram stores for the emoji:
  * video emoji  → ``video/webm``
  * animated     → Lottie JSON (TGS is gzipped Lottie; we decompress it)
  * static       → ``image/webp``

Anything that fails — slot unset, Telegram unreachable, file too large —
answers 404, and the page keeps the slot's Unicode fallback it already
rendered. Failures are remembered briefly so a broken ID is not re-fetched
on every page view.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response

from shared import bot_appearance
from shared.config import PROJECT_ROOT, settings
from shared.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)

CACHE_DIR = PROJECT_ROOT / ".cache" / "tg_emoji"
MAX_DOWNLOAD_BYTES = 512 * 1024
MAX_LOTTIE_BYTES = 2 * 1024 * 1024
FAILURE_TTL_SECONDS = 600
APPEARANCE_TTL_SECONDS = 5

_MEDIA = {
    "webm": "video/webm",
    "json": "application/json",
    "webp": "image/webp",
}

_failures: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


class EmojiUnavailable(Exception):
    pass


# --------------------------------------------------------------------------- #
# Appearance, cached like the bot's (5 s) so pages never wait on the DB twice
# --------------------------------------------------------------------------- #
class _WebAppearance:
    def __init__(self) -> None:
        self.value = bot_appearance.Appearance()
        self.expires = 0.0
        self.lock = asyncio.Lock()

    async def get(self) -> bot_appearance.Appearance:
        if time.monotonic() < self.expires:
            return self.value
        async with self.lock:
            if time.monotonic() >= self.expires:
                try:
                    async with AsyncSessionLocal() as session:
                        self.value = await bot_appearance.load_appearance(session)
                except Exception:  # noqa: BLE001 — never block a page on this
                    self.value = bot_appearance.Appearance(enabled=False)
                    bot_appearance.warn_once("web_appearance_load_failed")
                self.expires = time.monotonic() + APPEARANCE_TTL_SECONDS
        return self.value


appearance_cache = _WebAppearance()


def animated_id(appearance: bot_appearance.Appearance, key: str) -> str:
    """The slot's custom emoji ID if it should animate, else ``""``."""
    slot = appearance.slots.get(key)
    if slot is None or not (appearance.enabled and slot.enabled):
        return ""
    return slot.custom_emoji_id


# --------------------------------------------------------------------------- #
# Telegram fetch + disk cache
# --------------------------------------------------------------------------- #
async def _fetch_from_telegram(custom_id: str) -> tuple[bytes, str]:
    """Download one custom emoji. Returns ``(payload, extension)``."""
    from app.webshop.routes import _get_bot

    bot = _get_bot()
    stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=[custom_id])
    if len(stickers) != 1:
        raise EmojiUnavailable("unknown id")
    sticker = stickers[0]
    file = await bot.get_file(sticker.file_id)
    if not file.file_path or (file.file_size or 0) > MAX_DOWNLOAD_BYTES:
        raise EmojiUnavailable("missing or oversized file")
    buffer = await bot.download_file(file.file_path)
    data = buffer.read() if buffer is not None else b""
    if not data or len(data) > MAX_DOWNLOAD_BYTES:
        raise EmojiUnavailable("empty or oversized download")

    if sticker.is_video:
        return data, "webm"
    if sticker.is_animated:
        try:
            lottie = gzip.decompress(data)
        except OSError as exc:
            raise EmojiUnavailable("bad tgs") from exc
        if len(lottie) > MAX_LOTTIE_BYTES:
            raise EmojiUnavailable("oversized lottie")
        return lottie, "json"
    return data, "webp"


def _cached(custom_id: str) -> Path | None:
    for ext in _MEDIA:
        path = CACHE_DIR / f"{custom_id}.{ext}"
        if path.is_file():
            return path
    return None


async def load(custom_id: str) -> tuple[bytes, str]:
    """Bytes and media type for an emoji ID, from disk or Telegram."""
    path = _cached(custom_id)
    if path is None:
        failed_at = _failures.get(custom_id)
        if failed_at is not None and time.monotonic() - failed_at < FAILURE_TTL_SECONDS:
            raise EmojiUnavailable("recently failed")
        lock = _locks.setdefault(custom_id, asyncio.Lock())
        async with lock:
            path = _cached(custom_id)
            if path is None:
                try:
                    data, ext = await _fetch_from_telegram(custom_id)
                except EmojiUnavailable:
                    _failures[custom_id] = time.monotonic()
                    raise
                except Exception as exc:  # noqa: BLE001 — network/API errors
                    _failures[custom_id] = time.monotonic()
                    bot_appearance.warn_once("web_emoji_fetch_failed")
                    raise EmojiUnavailable("fetch failed") from exc
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                path = CACHE_DIR / f"{custom_id}.{ext}"
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
                _failures.pop(custom_id, None)
    return path.read_bytes(), _MEDIA[path.suffix.lstrip(".")]


@router.get("/web/emoji/{slot}")
async def emoji_asset(slot: str) -> Response:
    if slot not in bot_appearance.DEFAULTS or not settings.bot_token:
        return Response(status_code=404)
    custom_id = animated_id(await appearance_cache.get(), slot)
    if not custom_id:
        return Response(status_code=404)
    try:
        data, media_type = await load(custom_id)
    except EmojiUnavailable:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    # Pages request ?v=<custom id>, so a changed slot is a new URL and the
    # browser may keep each version for a day without going stale.
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
