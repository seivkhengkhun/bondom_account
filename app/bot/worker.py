"""Worker entrypoint for managed platforms (Railway/Render/Fly)."""

import asyncio

from app.bot.runner import run_bot


if __name__ == "__main__":
    asyncio.run(run_bot())
