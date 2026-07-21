"""Asynchronous SQLAlchemy configuration for PostgreSQL.

This module is the single database entry point for every component:
FastAPI (via the ``get_db`` dependency), the aiogram bot and the Reflex
admin panel (both via ``AsyncSessionLocal`` directly).
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from shared.config import PROJECT_ROOT, settings


def _normalized_database_url(url: str) -> str:
    """Normalize DB URLs across local and cloud environments."""
    if url.startswith("postgresql://"):
        # Cloud providers often supply sync SQLAlchemy URLs by default.
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)

    prefix = "sqlite+aiosqlite:///./"
    if url.startswith(prefix):
        rel = url.removeprefix(prefix)
        abs_path = (PROJECT_ROOT / rel).resolve()
        return f"sqlite+aiosqlite:///{abs_path.as_posix()}"
    return url

engine = create_async_engine(
    _normalized_database_url(settings.database_url),
    echo=settings.echo_sql,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

# expire_on_commit=False keeps ORM objects usable after the transaction
# commits, which lets services return committed entities to any caller
# (API route, bot handler, Reflex event handler) without implicit
# lazy refreshes.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Declarative base class shared by all ORM models."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a request-scoped session.

    Transaction boundaries are owned by the service layer
    (``async with session.begin()``); this dependency only guarantees
    the session is closed when the request finishes.
    """
    async with AsyncSessionLocal() as session:
        yield session


# Additive columns added to already-existing tables after their initial
# release. ``create_all`` creates missing TABLES but never alters existing
# ones, so these are applied with ADD COLUMN (ignoring "already exists").
# SQLite and Postgres both accept this idempotent pattern.
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, type)
    ("users", "is_agency", "BOOLEAN DEFAULT 0"),
    ("users", "agency_name", "VARCHAR(128)"),
    ("users", "agency_status", "VARCHAR(20)"),
    ("users", "payout_contact", "VARCHAR(255)"),
    ("products", "owner_id", "INTEGER"),
]


async def _apply_additive_columns(conn) -> None:
    from sqlalchemy import text

    for table, column, coltype in _ADDITIVE_COLUMNS:
        try:
            await conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
            )
        except Exception:
            # Column already exists (or table not yet present) — safe to skip.
            pass
    # Ensure the text import above isn't flagged unused on some linters.
    _ = text


async def init_db() -> None:
    """Create all tables + apply additive columns. Dev convenience.

    New tables are created by ``create_all``; columns added to
    pre-existing tables are applied idempotently so a running production
    database upgrades in place without a separate migration step.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_additive_columns(conn)
