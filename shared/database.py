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


async def init_db() -> None:
    """Create all tables. Dev convenience — use Alembic in production."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
