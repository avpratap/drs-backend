from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings


def _engine_kwargs() -> dict:
    kw: dict = {"pool_pre_ping": True, "future": True}
    connect_args: dict = {}
    if settings.DB_POOL_MODE == "pgbouncer":
        # Transaction pooling can't keep server-side prepared statements; use a
        # null app-side pool and turn off asyncpg's statement cache.
        kw["poolclass"] = NullPool
        connect_args["statement_cache_size"] = 0
    else:
        kw["pool_size"] = settings.DB_POOL_SIZE
        kw["max_overflow"] = settings.DB_MAX_OVERFLOW
        kw["pool_recycle"] = 1800
    if settings.DB_SSL:
        # Require TLS to the database (managed Postgres in prod). asyncpg accepts a
        # bool/str/SSLContext here; True uses the default verifying context.
        connect_args["ssl"] = True
    if connect_args:
        kw["connect_args"] = connect_args
    return kw


engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs())
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
