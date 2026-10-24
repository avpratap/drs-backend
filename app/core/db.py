from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import ORMExecuteState, Session
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
        # bool/str/SSLContext here; True uses the default (system-CA) verifying context.
        if settings.DB_SSL_ROOT_CERT:
            # Verify the server cert against an explicit CA bundle (e.g. the AWS RDS CA).
            # Fails fast at import if the path is missing/unreadable — a clear boot error
            # beats silently falling back to an unverified connection.
            import ssl

            connect_args["ssl"] = ssl.create_default_context(
                cafile=settings.DB_SSL_ROOT_CERT
            )
        else:
            connect_args["ssl"] = True
    if connect_args:
        kw["connect_args"] = connect_args
    return kw


engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs())
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(Session, "do_orm_execute")
def _locked_reads_refresh(state: ORMExecuteState) -> None:
    """A SELECT ... FOR UPDATE must load the row as the lock sees it. With
    expire_on_commit=False the identity map would otherwise hand back a copy read
    before an earlier commit in this session (e.g. a wallet balance read before a
    refund's gateway call), and the read-modify-write would lose a concurrent update."""
    if state.is_select and getattr(state.statement, "_for_update_arg", None) is not None:
        state.update_execution_options(populate_existing=True)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
