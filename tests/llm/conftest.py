from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException
from support.database import ALEMBIC_INI
from testcontainers.community.postgres import PostgresContainer

PGVECTOR_IMAGE = "pgvector/pgvector:pg17"


@pytest.fixture(scope="session")
def migrated_database_url() -> Iterator[str]:
    """A throwaway Postgres for the tests that record real usage; without Docker the test FAILS."""
    try:
        container = PostgresContainer(PGVECTOR_IMAGE, driver="asyncpg")
        container.start()
    except (DockerException, OSError) as error:
        pytest.fail(f"Docker is not reachable, these tests need a real Postgres: {error}")
    try:
        database_url = container.get_connection_url()
        config = Config(str(ALEMBIC_INI))
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        yield database_url
    finally:
        container.stop()
