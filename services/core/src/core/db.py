"""Async engine + session factory.

One engine per process, one session per request. The URL drives which driver
loads — `sqlite+aiosqlite://` or `postgresql+asyncpg://` — so the same code
works against both backends. Pool flags only make sense for server DBs; SQLite
gets the defaults (single connection, no pool).
"""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from service_kit.config import Settings


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def make_engine(settings: Settings) -> AsyncEngine:
    url = settings.resolved_database_url
    if _is_sqlite(url):
        return create_async_engine(url, future=True)
    return create_async_engine(
        url,
        future=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_timeout=settings.db_pool_timeout_seconds,
        connect_args={"command_timeout": settings.db_query_timeout_seconds},
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
