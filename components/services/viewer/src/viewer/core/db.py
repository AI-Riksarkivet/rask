"""Async engine + session factory.

One engine per process, one session per request. The URL drives which driver
loads — `sqlite+aiosqlite://` or `postgresql+asyncpg://` — so the same code
works against both backends. Pool flags only make sense for server DBs; SQLite
gets the defaults (single connection, no pool).
"""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.core.config import Settings


SQLModel.metadata.naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


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
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
