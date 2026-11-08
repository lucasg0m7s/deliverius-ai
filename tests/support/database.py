from pathlib import Path
from typing import Any

import asyncpg

from deliverius.config import Settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def settings_for(database_url: str, **overrides: Any) -> Settings:
    """Settings for a test database, ignoring any local `.env`."""
    return Settings(_env_file=None, database_url=database_url, **overrides)  # type: ignore[call-arg]


async def create_database(admin_dsn: str, name: str) -> None:
    connection = await asyncpg.connect(admin_dsn)
    try:
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


async def drop_database(admin_dsn: str, name: str) -> None:
    connection = await asyncpg.connect(admin_dsn)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await connection.close()
