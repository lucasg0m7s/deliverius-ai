import asyncio
from collections.abc import Iterator

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from support.database import ALEMBIC_INI, create_database, drop_database
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration

DOMAIN_TABLES = {
    "customers",
    "restaurants",
    "orders",
    "order_items",
    "delivery_events",
    "audit_log",
}
LLM_TABLES = {"llm_calls", "llm_budget_usage"}
POLICY_SEARCH_TABLES = {"policy_chunks"}
ACTION_TABLES = {"conversations", "approval_requests", "handoffs", "actions"}
ALL_TABLES = DOMAIN_TABLES | LLM_TABLES | POLICY_SEARCH_TABLES | ACTION_TABLES


@pytest.fixture
def fresh_database_url(postgres_container: PostgresContainer) -> Iterator[str]:
    name = "migration_round_trip"
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


async def public_tables(database_url: str) -> set[str]:
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    finally:
        await connection.close()
    return {row["tablename"] for row in rows} - {"alembic_version"}


async def search_objects(database_url: str) -> tuple[bool, bool]:
    """Whether the `unaccent` extension and the `pt_unaccent` configuration exist."""
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    connection = await asyncpg.connect(dsn)
    try:
        extension = await connection.fetchval(
            "SELECT 1 FROM pg_extension WHERE extname = 'unaccent'"
        )
        configuration = await connection.fetchval(
            "SELECT 1 FROM pg_ts_config WHERE cfgname = 'pt_unaccent'"
        )
    finally:
        await connection.close()
    return extension is not None, configuration is not None


def migration_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_upgrade_downgrade_upgrade_round_trip(fresh_database_url: str) -> None:
    config = migration_config(fresh_database_url)

    command.upgrade(config, "head")
    assert asyncio.run(public_tables(fresh_database_url)) == ALL_TABLES

    command.downgrade(config, "0001")
    assert asyncio.run(public_tables(fresh_database_url)) == set()

    command.upgrade(config, "head")
    assert asyncio.run(public_tables(fresh_database_url)) == ALL_TABLES


def test_llm_migration_downgrade_removes_only_its_tables(fresh_database_url: str) -> None:
    config = migration_config(fresh_database_url)
    command.upgrade(config, "head")

    command.downgrade(config, "0002")

    assert asyncio.run(public_tables(fresh_database_url)) == DOMAIN_TABLES


def test_policy_search_migration_downgrade_removes_only_its_objects(
    fresh_database_url: str,
) -> None:
    config = migration_config(fresh_database_url)
    command.upgrade(config, "head")
    assert asyncio.run(search_objects(fresh_database_url)) == (True, True)

    command.downgrade(config, "0003")

    assert asyncio.run(public_tables(fresh_database_url)) == DOMAIN_TABLES | LLM_TABLES
    assert asyncio.run(search_objects(fresh_database_url)) == (False, False)

    command.upgrade(config, "head")
    assert asyncio.run(search_objects(fresh_database_url)) == (True, True)


async def public_functions(database_url: str) -> set[str]:
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch(
            "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
        )
    finally:
        await connection.close()
    return {row["proname"] for row in rows}


def test_actions_migration_downgrade_removes_only_its_objects(fresh_database_url: str) -> None:
    config = migration_config(fresh_database_url)
    command.upgrade(config, "head")
    assert "approval_requests_reject_decided_change" in asyncio.run(
        public_functions(fresh_database_url)
    )

    command.downgrade(config, "0004")

    assert asyncio.run(public_tables(fresh_database_url)) == (
        DOMAIN_TABLES | LLM_TABLES | POLICY_SEARCH_TABLES
    )
    assert "approval_requests_reject_decided_change" not in asyncio.run(
        public_functions(fresh_database_url)
    )
    assert "audit_log_reject_change" in asyncio.run(public_functions(fresh_database_url))

    command.upgrade(config, "head")
    assert asyncio.run(public_tables(fresh_database_url)) == ALL_TABLES


async def public_indexes(database_url: str) -> set[str]:
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        )
    finally:
        await connection.close()
    return {row["indexname"] for row in rows}


def test_fraud_index_migration_round_trip(fresh_database_url: str) -> None:
    config = migration_config(fresh_database_url)
    index = "ix_approval_requests_sandbox_id_customer_id_claimed_at"
    command.upgrade(config, "head")
    assert index in asyncio.run(public_indexes(fresh_database_url))

    command.downgrade(config, "0005")

    remaining = asyncio.run(public_indexes(fresh_database_url))
    assert index not in remaining
    assert "ix_approval_requests_customer_id" in remaining
    assert asyncio.run(public_tables(fresh_database_url)) == ALL_TABLES

    command.upgrade(config, "head")
    assert index in asyncio.run(public_indexes(fresh_database_url))
