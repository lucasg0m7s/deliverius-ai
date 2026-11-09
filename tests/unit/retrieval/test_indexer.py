"""Incremental indexing, on an in-memory store with the lock semantics of the real one."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from structlog.testing import capture_logs
from support.retrieval_doubles import FakeEmbedder, InMemoryChunkStore

from deliverius.policy import POLICY_LIMITS, PolicyDocument, PolicyLimits, load_policy_documents
from deliverius.retrieval.chunking import chunk_policy_documents
from deliverius.retrieval.indexer import PolicyIndexer
from deliverius.shared.clock import FixedClock

DOCUMENTS = load_policy_documents()
ALL_IDS = tuple(str(doc.id) for doc in DOCUMENTS)
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def indexer(
    store: InMemoryChunkStore,
    embedder: FakeEmbedder,
    *,
    documents: Sequence[PolicyDocument] | None = None,
    limits: PolicyLimits = POLICY_LIMITS,
    clock: FixedClock | None = None,
) -> PolicyIndexer:
    return PolicyIndexer(
        store,
        embedder,
        clock=clock or FixedClock(NOW),
        documents=documents,
        limits=limits,
    )


async def test_the_first_run_indexes_every_chunk_of_every_document() -> None:
    store, embedder = InMemoryChunkStore(), FakeEmbedder()

    report = await indexer(store, embedder).reindex()

    assert report.recalculated == ALL_IDS
    assert report.kept == ()
    assert report.removed == ()
    assert len(store.chunk_ids) == 48
    assert "POL-03#prazo" in store.chunk_ids
    assert embedder.passages_embedded == 48


async def test_the_chunks_are_stored_with_the_time_of_the_run() -> None:
    store = InMemoryChunkStore()

    await indexer(store, FakeEmbedder()).reindex()

    assert {stored.indexed_at for stored in store.documents.values()} == {NOW}


async def test_a_second_run_without_changes_embeds_nothing_and_writes_nothing() -> None:
    store, embedder = InMemoryChunkStore(), FakeEmbedder()
    await indexer(store, embedder).reindex()
    store.events.clear()
    store.replaced.clear()

    report = await indexer(store, embedder).reindex()

    assert report.recalculated == ()
    assert report.removed == ()
    assert report.kept == ALL_IDS
    assert embedder.passages_embedded == 48
    assert store.events == []
    assert store.replaced == []


async def test_a_changed_limit_recomputes_only_the_documents_that_show_it() -> None:
    store, embedder = InMemoryChunkStore(), FakeEmbedder()
    await indexer(store, embedder).reindex()
    store.replaced.clear()
    longer = replace(POLICY_LIMITS, item_claim_window=timedelta(hours=60))

    report = await indexer(
        store, embedder, limits=longer, clock=FixedClock(NOW + timedelta(hours=1))
    ).reindex()

    assert report.recalculated == ("POL-03", "POL-04", "POL-05")
    assert store.replaced == ["POL-03", "POL-04", "POL-05"]
    assert embedder.passages_embedded == 48 + 12
    assert set(report.kept) == set(ALL_IDS) - {"POL-03", "POL-04", "POL-05"}
    deadline = store.documents["POL-03"].document.chunks[2]
    assert "60 horas" in deadline.content
    assert store.documents["POL-03"].indexed_at == NOW + timedelta(hours=1)
    assert store.documents["POL-01"].indexed_at == NOW


async def test_a_new_model_revision_recomputes_every_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, embedder = InMemoryChunkStore(), FakeEmbedder()
    await indexer(store, embedder).reindex()
    monkeypatch.setattr("deliverius.retrieval.chunking.MODEL_REVISION", "another-revision")

    report = await indexer(store, embedder).reindex()

    assert report.recalculated == ALL_IDS
    assert report.kept == ()
    assert embedder.passages_embedded == 48 * 2


async def test_a_document_that_no_longer_exists_is_removed() -> None:
    store, embedder = InMemoryChunkStore(), FakeEmbedder()
    await indexer(store, embedder).reindex()

    report = await indexer(store, embedder, documents=DOCUMENTS[:11]).reindex()

    assert report.removed == ("POL-12",)
    assert report.recalculated == ()
    assert "POL-12" not in store.documents
    assert len(store.chunk_ids) == 44
    assert embedder.passages_embedded == 48


async def test_a_section_that_no_longer_exists_disappears_with_the_new_version() -> None:
    store, embedder = InMemoryChunkStore(), FakeEmbedder()
    await indexer(store, embedder).reindex()
    first = DOCUMENTS[0]
    shorter = replace(first, markdown=first.markdown.split("\n## ")[0] + "\n## Prazo\n\nUm.\n")

    report = await indexer(store, embedder, documents=[shorter, *DOCUMENTS[1:]]).reindex()

    assert report.recalculated == ("POL-01",)
    assert [c.chunk_id for c in store.documents["POL-01"].document.chunks] == ["POL-01#prazo"]
    assert not [i for i in store.chunk_ids if i.startswith("POL-01#quando")]


async def test_the_embeddings_are_computed_before_the_lock_is_taken() -> None:
    store = InMemoryChunkStore()

    await indexer(store, FakeEmbedder(store.events)).reindex()

    assert store.events == ["embed", "lock"]


@dataclass
class _RaceStore(InMemoryChunkStore):
    """The first read says "nothing indexed"; by the lock, another indexer has done everything."""

    outside_reads: int = 0

    async def read_hashes(self) -> dict[str, str]:
        self.outside_reads += 1
        if self.outside_reads == 1:
            return {}
        return await super().read_hashes()


async def test_hashes_are_read_again_inside_the_lock_and_finished_work_is_skipped() -> None:
    store, embedder = _RaceStore(), FakeEmbedder()
    other = indexer(store, FakeEmbedder())
    await other.reindex()
    store.outside_reads = 0
    store.replaced.clear()

    report = await indexer(store, embedder).reindex()

    assert report.recalculated == ()
    assert store.replaced == []
    assert embedder.passages_embedded == 48


async def test_two_runs_at_once_write_every_document_once() -> None:
    store = InMemoryChunkStore()

    first, second = await asyncio.gather(
        indexer(store, FakeEmbedder()).reindex(), indexer(store, FakeEmbedder()).reindex()
    )

    assert sorted([*first.recalculated, *second.recalculated]) == sorted(ALL_IDS)
    assert sorted(store.replaced) == sorted(ALL_IDS)
    assert len(store.chunk_ids) == len(set(store.chunk_ids)) == 48


async def test_the_run_is_logged_with_documents_and_never_with_text() -> None:
    store = InMemoryChunkStore()
    await indexer(store, FakeEmbedder()).reindex()

    with capture_logs() as logs:
        await indexer(store, FakeEmbedder(), documents=DOCUMENTS[:11]).reindex()

    [event] = [log for log in logs if log["event"] == "retrieval.indexed"]
    assert event["recalculated"] == []
    assert event["kept_count"] == 11
    assert event["removed"] == ["POL-12"]


async def test_the_chunks_written_match_the_chunking_of_the_documents() -> None:
    store = InMemoryChunkStore()

    await indexer(store, FakeEmbedder()).reindex()

    expected = {doc.document_id: doc for doc in chunk_policy_documents()}
    assert {k: v.document for k, v in store.documents.items()} == expected
