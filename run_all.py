"""Supervisor: initialize the database, then run the FastAPI server and
the aiogram bot concurrently in one asyncio event loop.

Usage:
    python run_all.py            # API + bot
    python run_all.py --with-web # also spawn the Reflex admin panel

The Reflex dev server manages its own processes, so it is spawned as a
subprocess rather than a coroutine; it can equally be run by hand with
`reflex run` from app/web/.
"""

import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import uvicorn

from shared.config import settings
from shared.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("run_all")

WEB_DIR = Path(__file__).resolve().parent / "app" / "web"


async def run_api() -> None:
    config = uvicorn.Config(
        "app.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    await uvicorn.Server(config).serve()


async def async_main() -> None:
    logger.info("Initializing database tables…")
    await init_db()

    from app.bot.runner import run_bot  # imported late so .env is loaded first

    logger.info(
        "Starting API on http://%s:%s and Telegram bot…",
        settings.api_host,
        settings.api_port,
    )
    await asyncio.gather(run_api(), run_bot())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-web",
        action="store_true",
        help="also start the Reflex admin panel (reflex run in app/web)",
    )
    args = parser.parse_args()

    web_process: subprocess.Popen[bytes] | None = None
    if args.with_web:
        logger.info("Spawning Reflex admin panel (reflex run in %s)…", WEB_DIR)
        web_process = subprocess.Popen(
            [sys.executable, "-m", "reflex", "run"], cwd=WEB_DIR
        )

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        if web_process is not None:
            web_process.terminate()
            web_process.wait(timeout=15)


if __name__ == "__main__":
    main()
