"""Indexing on Postgres: the advisory lock, the swap of a document, and the race of two indexers."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from support.retrieval_database import clear_policy_chunks, repository_for
from support.retrieval_doubles import FakeEmbedder

from deliverius.db.errors import DatabaseTimeoutError
from deliverius.policy import POLICY_LIMITS, PolicyLimits
from deliverius.retrieval import PolicyIndexer
from deliverius.retrieval.repository import INDEX_LOCK_KEY

pytestmark = pytest.mark.integration

CHUNKS = 48
CITING_ITEM_CLAIM_WINDOW = {"POL-03", "POL-04", "POL-05"}
LONGER_ITEM_WINDOW = replace(POLICY_LIMITS, item_claim_window=timedelta(hours=60))


@pytest.fixture(autouse=True)
async def empty_table(engine: AsyncEngine) -> AsyncIterator[None]:
    await clear_policy_chunks(engine)
    yield
    await clear_policy_chunks(engine)


def indexer(
    engine: AsyncEngine, embedder: FakeEmbedder | None = None, limits: PolicyLimits = POLICY_LIMITS
) -> PolicyIndexer:
    return PolicyIndexer(repository_for(engine), embedder or FakeEmbedder(), limits=limits)


async def rows(engine: AsyncEngine) -> list[tuple[str, str, str]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT chunk_id, document_id, document_hash FROM policy_chunks ORDER BY chunk_id")
        )
        return [(row.chunk_id, row.document_id, row.document_hash) for row in result]


async def indexed_at(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT document_id, min(indexed_at) AS at FROM policy_chunks GROUP BY 1")
        )
        return {row.document_id: row.at for row in result}


async def test_the_first_run_writes_every_chunk(engine: AsyncEngine) -> None:
    report = await indexer(engine).reindex()

    assert len(report.recalculated) == 12
    stored = await rows(engine)
    assert len(stored) == CHUNKS
    assert "POL-03#prazo" in {chunk_id for chunk_id, _, _ in stored}


async def test_a_second_run_changes_no_row_and_embeds_nothing(engine: AsyncEngine) -> None:
    await indexer(engine).reindex()
    before = await indexed_at(engine)
    embedder = FakeEmbedder()

    report = await indexer(engine, embedder).reindex()

    assert report.recalculated == ()
    assert len(report.kept) == 12
    assert embedder.passages_embedded == 0
    assert await indexed_at(engine) == before


async def test_two_indexings_at_once_on_an_empty_table_write_each_chunk_once(
    engine: AsyncEngine,
) -> None:
    first, second = await asyncio.gather(indexer(engine).reindex(), indexer(engine).reindex())

    stored = await rows(engine)
    assert len(stored) == CHUNKS
    assert len({chunk_id for chunk_id, _, _ in stored}) == CHUNKS
    assert len(first.recalculated) + len(second.recalculated) == 12
    assert set(first.recalculated).isdisjoint(second.recalculated)


async def test_many_indexings_at_once_still_write_each_chunk_once(engine: AsyncEngine) -> None:
    reports = await asyncio.gather(*(indexer(engine).reindex() for _ in range(5)))

    assert len(await rows(engine)) == CHUNKS
    assert sum(len(report.recalculated) for report in reports) == 12


async def test_a_document_changed_between_two_runs_rewrites_only_the_documents_that_show_it(
    engine: AsyncEngine,
) -> None:
    await indexer(engine).reindex()
    before = await indexed_at(engine)

    report = await indexer(engine, limits=LONGER_ITEM_WINDOW).reindex()

    assert set(report.recalculated) == CITING_ITEM_CLAIM_WINDOW
    after = await indexed_at(engine)
    assert {doc for doc in after if after[doc] != before[doc]} == CITING_ITEM_CLAIM_WINDOW
    assert len(await rows(engine)) == CHUNKS
    async with engine.connect() as connection:
        content = (
            await connection.execute(
                text("SELECT content FROM policy_chunks WHERE chunk_id = 'POL-03#prazo'")
            )
        ).scalar_one()
    assert "60 horas" in content


async def test_two_indexings_with_a_changed_document_between_them_leave_one_consistent_version(
    engine: AsyncEngine,
) -> None:
    await asyncio.gather(
        indexer(engine).reindex(), indexer(engine, limits=LONGER_ITEM_WINDOW).reindex()
    )

    stored = await rows(engine)
    assert len(stored) == CHUNKS
    assert len({chunk_id for chunk_id, _, _ in stored}) == CHUNKS
    hashes_by_document: dict[str, set[str]] = {}
    for _, document_id, document_hash in stored:
        hashes_by_document.setdefault(document_id, set()).add(document_hash)
    assert all(len(hashes) == 1 for hashes in hashes_by_document.values())


async def test_a_document_that_no_longer_exists_is_deleted(engine: AsyncEngine) -> None:
    await indexer(engine).reindex()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO policy_chunks (chunk_id, document_id, section, position, content, "
                "document_hash, embedding, indexed_at) "
                "SELECT 'POL-99#old', 'POL-99', 'Old', 1, 'old text', 'x', embedding, now() "
                "FROM policy_chunks LIMIT 1"
            )
        )

    report = await indexer(engine).reindex()

    assert report.removed == ("POL-99",)
    assert "POL-99" not in {document_id for _, document_id, _ in await rows(engine)}


async def test_an_indexer_that_cannot_get_the_lock_times_out_and_writes_nothing(
    engine: AsyncEngine,
) -> None:
    holder = await engine.connect()
    try:
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": INDEX_LOCK_KEY})
        blocked = PolicyIndexer(repository_for(engine, lock_timeout_ms=200), FakeEmbedder())

        with pytest.raises(DatabaseTimeoutError):
            await blocked.reindex()
    finally:
        await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": INDEX_LOCK_KEY})
        await holder.close()

    assert await rows(engine) == []
    await indexer(engine).reindex()
    assert len(await rows(engine)) == CHUNKS
