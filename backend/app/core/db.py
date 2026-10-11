from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

connect_args = {}
if settings.is_sqlite:
    connect_args = {"check_same_thread": False}

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    future=True,
    connect_args=connect_args,
)

# SQLite is a single-writer database, and this app can legitimately have two
# writers for a moment: an uvicorn worker plus a leftover process from an
# earlier launch, or a task touching `tasks` while ingest writes progress.
# Without these pragmas the loser of that race fails instantly with
#   sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
# which surfaced as a progress bar that stopped at a number and never moved.
#
# WAL lets readers run while a writer holds the file, and busy_timeout makes a
# blocked writer wait (10s) instead of raising on the first collision.
if settings.is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=10000")
        finally:
            cursor.close()


SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
