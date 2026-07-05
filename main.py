"""Single-file runtime entrypoint for the whole app.

Run this file in production to keep deployment simple:

    uvicorn main:app --host 0.0.0.0 --port 8000

This starts the FastAPI app and the Telegram bot in the same Python
process, so hosting only needs one web service plus one PostgreSQL
database.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextlib import suppress

from app.api.main import app
from app.bot.runner import run_bot
from shared.config import settings

logger = logging.getLogger(__name__)
_bot_task: asyncio.Task[None] | None = None
_previous_lifespan = app.router.lifespan_context


def _log_bot_task_result(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("Telegram bot task cancelled.")
    except Exception:
        logger.exception("Telegram bot task crashed.")


@asynccontextmanager
async def combined_lifespan(app_instance) -> AsyncIterator[None]:
    """Run the API lifespan and Telegram polling in the same process."""
    global _bot_task
    async with _previous_lifespan(app_instance):
        if not settings.bot_token:
            logger.warning("BOT_TOKEN is not set — Telegram bot disabled.")
        elif _bot_task is None or _bot_task.done():
            _bot_task = asyncio.create_task(run_bot())
            _bot_task.add_done_callback(_log_bot_task_result)

        try:
            yield
        finally:
            if _bot_task is not None:
                _bot_task.cancel()
                with suppress(asyncio.CancelledError):
                    await _bot_task
                _bot_task = None


app.router.lifespan_context = combined_lifespan


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )