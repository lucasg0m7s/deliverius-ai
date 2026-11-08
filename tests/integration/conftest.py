import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from support.database import ALEMBIC_INI, create_database, drop_database, settings_for
from testcontainers.community.postgres import PostgresContainer

from deliverius.db.engine import create_engine
from deliverius.db.session import create_session_factory

PGVECTOR_IMAGE = "pgvector/pgvector:pg17"


def _start_container() -> PostgresContainer:
    try:
        container = PostgresContainer(PGVECTOR_IMAGE, driver="asyncpg")
        container.start()
    except (DockerException, OSError) as error:
        pytest.skip(f"Docker is not reachable, integration tests need it: {error}")
    return container


def _migrate(database_url: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    container = _start_container()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def migrated_database_url(postgres_container: PostgresContainer) -> str:
    database_url = postgres_container.get_connection_url()
    _migrate(database_url)
    return database_url


@pytest.fixture
def database_without_pgvector_url(postgres_container: PostgresContainer) -> Iterator[str]:
    name = "without_pgvector"
    admin_dsn = postgres_container.get_connection_url(driver=None)
    asyncio.run(create_database(admin_dsn, name))
    try:
        yield (
            make_url(postgres_container.get_connection_url())
            .set(database=name)
            .render_as_string(hide_password=False)
        )
    finally:
        asyncio.run(drop_database(admin_dsn, name))


@pytest.fixture
async def engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings_for(migrated_database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction that is always rolled back: tests leave no rows."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
async def llm_usage_factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Committed sessions, as the real store uses: the LLM tables are cleaned before and after."""

    async def clean() -> None:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM llm_calls"))
            await connection.execute(text("DELETE FROM llm_budget_usage"))

    await clean()
    try:
        yield create_session_factory(engine)
    finally:
        await clean()
