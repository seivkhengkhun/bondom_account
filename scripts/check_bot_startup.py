"""Read-only startup preflight: no polling, messages, payments, or webhook changes."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def check() -> int:
    from sqlalchemy import inspect
    from shared.config import settings
    from shared.database import AsyncSessionLocal, engine
    from shared.bot_appearance import load_appearance
    from app.bot.appearance import create_bot
    from app.bot.runner import build_dispatcher

    bot = None
    try:
        if not settings.bot_token:
            print("FAIL: BOT_TOKEN is unset.")
            return 1
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
        required = {"app_settings", "users", "products", "orders", "inventory", "payments", "wallet_topups"}
        if not required.issubset(tables):
            print("FAIL: required database tables are missing; use the normal application startup.")
            return 1
        async with AsyncSessionLocal() as session:
            appearance = await load_appearance(session)
        dispatcher = build_dispatcher()
        bot = create_bot(settings.bot_token)
        bot.session.timeout = 15
        identity = await bot.get_me()
        webhook = await bot.get_webhook_info()
        print(f"PASS: database accessible, {len(appearance.slots)} emoji slots, dispatcher ready.")
        print(f"PASS: Telegram authenticated @{identity.username}.")
        print("INFO: webhook is configured; normal polling startup removes it." if webhook.url
              else "PASS: no webhook blocks polling.")
        print(f"PASS: {len(dispatcher.sub_routers)} routers loaded; startup preflight complete.")
        return 0
    except Exception as exc:
        # Never print exception strings: HTTP URLs can contain the bot token.
        print(f"FAIL: startup preflight failed ({type(exc).__name__}).")
        return 1
    finally:
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
