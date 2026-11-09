import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from support.database import ALEMBIC_INI, create_database, drop_database, settings_for
from support.retrieval_database import drop_policies, index_policies
from testcontainers.community.postgres import PostgresContainer

from deliverius.config import Settings
from deliverius.db.engine import create_engine
from deliverius.db.session import create_session_factory
from deliverius.retrieval import E5Embedder, ModelFileError

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


@pytest.fixture(scope="session")
def embedder() -> E5Embedder:
    """The real embedding model. Missing or wrong files fail the test: they never skip it."""
    model_dir = Settings(_env_file=None).retrieval_model_dir  # type: ignore[call-arg]
    try:
        return E5Embedder.load(Path(model_dir), threads=2)
    except ModelFileError as error:
        pytest.fail(
            f"the embedding model is not ready ({error}); "
            "run `python -m deliverius.cli retrieval fetch-model` first",
            pytrace=False,
        )


@pytest.fixture(scope="module")
def indexed_policies(migrated_database_url: str, embedder: E5Embedder) -> Iterator[str]:
    """The twelve policy documents indexed with the real model; the table is emptied afterwards.

    Other modules insert their own rows into `policy_chunks`, so nothing may outlive the module.
    """
    asyncio.run(index_policies(migrated_database_url, embedder))
    try:
        yield migrated_database_url
    finally:
        asyncio.run(drop_policies(migrated_database_url))
