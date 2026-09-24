"""Create Pandora integration tables without modifying existing tables/data.

Run only after a database backup. SQLAlchemy create_all is intentionally used
because this integration is table-additive: it emits CREATE TABLE/INDEX for
missing objects and does not ALTER, DROP, truncate, or rewrite existing rows.
"""

import asyncio
import sys
from pathlib import Path

from sqlalchemy import inspect

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.database import Base, engine
import shared.models  # noqa: F401 - registers every model with Base.metadata


PANDORA_TABLES = {
    "supplier_products",
    "supplier_settings",
    "supplier_fulfillments",
    "supplier_webhook_events",
    "supplier_sync_runs",
}


async def main() -> None:
    async with engine.begin() as connection:
        before = set(await connection.run_sync(lambda c: inspect(c).get_table_names()))
        await connection.run_sync(Base.metadata.create_all)
        after = set(await connection.run_sync(lambda c: inspect(c).get_table_names()))
    created = sorted((after - before) & PANDORA_TABLES)
    missing = sorted(PANDORA_TABLES - after)
    print("Created:", ", ".join(created) if created else "none (already present)")
    print("Verified:", ", ".join(sorted(PANDORA_TABLES & after)))
    if missing:
        raise SystemExit("Missing after migration: " + ", ".join(missing))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
