from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from deliverius.config import Settings
from deliverius.db.errors import translate_db_errors


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.database_connect_timeout_seconds,
            "server_settings": server_settings(settings),
        },
    )


def server_settings(settings: Settings) -> dict[str, str]:
    """Limits the server applies to every connection, so nothing waits forever."""
    return {
        "statement_timeout": str(settings.database_statement_timeout_ms),
        "lock_timeout": str(settings.database_lock_timeout_ms),
        "idle_in_transaction_session_timeout": str(
            settings.database_idle_in_transaction_timeout_ms
        ),
    }


class DatabaseHealthProbe:
    """Checks the real database through the application's engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check_database(self) -> None:
        await self._scalar("SELECT 1")

    async def has_pgvector(self) -> bool:
        return bool(
            await self._scalar(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
            )
        )

    async def _scalar(self, statement: str) -> object:
        with translate_db_errors():
            async with self._engine.connect() as connection:
                return (await connection.execute(text(statement))).scalar_one()
