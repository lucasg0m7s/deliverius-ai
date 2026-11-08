import pytest
from sqlalchemy import text
from support.database import settings_for

from deliverius.db.engine import create_engine
from deliverius.db.errors import DatabaseTimeoutError, translate_db_errors

pytestmark = pytest.mark.integration


async def test_query_above_statement_timeout_is_interrupted(migrated_database_url: str) -> None:
    engine = create_engine(settings_for(migrated_database_url, database_statement_timeout_ms=100))
    try:
        with pytest.raises(DatabaseTimeoutError), translate_db_errors():
            async with engine.connect() as connection:
                await connection.execute(text("SELECT pg_sleep(2)"))
    finally:
        await engine.dispose()


async def test_lock_wait_above_lock_timeout_is_interrupted(migrated_database_url: str) -> None:
    engine = create_engine(settings_for(migrated_database_url, database_lock_timeout_ms=100))
    try:
        async with engine.connect() as holder, engine.connect() as waiter:
            await holder.execute(text("LOCK TABLE restaurants IN ACCESS EXCLUSIVE MODE"))
            with pytest.raises(DatabaseTimeoutError), translate_db_errors():
                await waiter.execute(text("SELECT count(*) FROM restaurants"))
    finally:
        await engine.dispose()


async def test_exhausted_pool_fails_fast_instead_of_waiting(migrated_database_url: str) -> None:
    engine = create_engine(
        settings_for(
            migrated_database_url,
            database_pool_size=1,
            database_max_overflow=0,
            database_pool_timeout_seconds=0.2,
        )
    )
    try:
        async with engine.connect():
            with pytest.raises(DatabaseTimeoutError), translate_db_errors():
                async with engine.connect():
                    pass
    finally:
        await engine.dispose()
