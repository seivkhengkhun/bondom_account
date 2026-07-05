"""Bot bootstrap — builds the Bot/Dispatcher and runs long polling."""

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import router
from shared.config import settings

logger = logging.getLogger(__name__)


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


async def run_bot() -> None:
    """Start long polling. No-op (with a warning) if BOT_TOKEN is unset,

    so `run_all.py` can still bring up the API while the bot is not yet
    configured.
    """
    if not settings.bot_token:
        logger.warning("BOT_TOKEN is not set — Telegram bot disabled.")
        return

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = build_dispatcher()
    logger.info("Starting Telegram bot polling…")
    await dispatcher.start_polling(bot)
