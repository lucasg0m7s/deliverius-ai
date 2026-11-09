"""Helpers of the retrieval integration tests: index the policies, clean the table, search."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from support.database import settings_for

from deliverius.config import Settings
from deliverius.db.engine import create_engine
from deliverius.db.session import create_session_factory
from deliverius.retrieval import E5Embedder, PolicyIndexer, PolicySearch, ReliabilityRule
from deliverius.retrieval.repository import PostgresPolicyChunkRepository

LOCK_TIMEOUT_MS = 2_000


def repository_for(
    engine: AsyncEngine, lock_timeout_ms: int = LOCK_TIMEOUT_MS
) -> PostgresPolicyChunkRepository:
    return PostgresPolicyChunkRepository(create_session_factory(engine), lock_timeout_ms)


def search_for(
    engine: AsyncEngine,
    embedder: E5Embedder,
    min_similarity: float | None = None,
    rule: ReliabilityRule | None = None,
) -> PolicySearch:
    """Without arguments, the measured defaults of `Settings` (docs/busca-politicas.md)."""
    defaults = Settings.model_fields
    return PolicySearch(
        embedder,
        repository_for(engine),
        defaults["retrieval_min_similarity"].default if min_similarity is None else min_similarity,
        ReliabilityRule(defaults["retrieval_reliability_rule"].default) if rule is None else rule,
    )


async def clear_policy_chunks(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM policy_chunks"))


async def index_policies(database_url: str, embedder: E5Embedder) -> None:
    """Indexes the real documents with the real model, on a connection of its own."""
    engine = create_engine(settings_for(database_url))
    try:
        await clear_policy_chunks(engine)
        await PolicyIndexer(repository_for(engine), embedder).reindex()
    finally:
        await engine.dispose()


async def drop_policies(database_url: str) -> None:
    engine = create_engine(settings_for(database_url))
    try:
        await clear_policy_chunks(engine)
    finally:
        await engine.dispose()
