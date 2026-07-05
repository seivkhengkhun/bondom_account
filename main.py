"""Single-file runtime entrypoint for the whole app.

Run this file in production to keep deployment simple:

    uvicorn main:app --host 0.0.0.0 --port 8000

This starts the FastAPI app and the Telegram bot in the same Python
process, so hosting only needs one web service plus one PostgreSQL
database.
"""

import asyncio
import logging
from contextlib import suppress

from app.api.main import app
from app.bot.runner import run_bot
from shared.config import settings

logger = logging.getLogger(__name__)
_bot_task: asyncio.Task[None] | None = None


@app.on_event("startup")
async def start_bot() -> None:
    """Start Telegram polling alongside the API."""
    global _bot_task
    if not settings.bot_token:
        logger.warning("BOT_TOKEN is not set — Telegram bot disabled.")
        return
    if _bot_task is None or _bot_task.done():
        _bot_task = asyncio.create_task(run_bot())


@app.on_event("shutdown")
async def stop_bot() -> None:
    """Stop Telegram polling cleanly when the web process exits."""
    global _bot_task
    if _bot_task is None:
        return
    _bot_task.cancel()
    with suppress(asyncio.CancelledError):
        await _bot_task
    _bot_task = None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )