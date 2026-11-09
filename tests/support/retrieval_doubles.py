"""Test doubles of the retrieval ports: an embedder, a chunk store and a searcher, all in memory."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from deliverius.retrieval.chunking import ChunkedDocument
from deliverius.retrieval.embedder import EMBEDDING_DIMENSIONS, Embeddings
from deliverius.retrieval.indexer import ChunkTransaction
from deliverius.retrieval.search import RankedChunks, SearchHit, SearchMode


class FakeEmbedder:
    """A vector per text, from its hash: the same text always gives the same unit vector."""

    def __init__(self, events: list[str] | None = None) -> None:
        self._events = events if events is not None else []
        self.passage_batches: list[list[str]] = []
        self.query_batches: list[list[str]] = []

    def embed_passages(self, texts: Sequence[str]) -> Embeddings:
        self._events.append("embed")
        self.passage_batches.append(list(texts))
        return _vectors(texts)

    def embed_queries(self, texts: Sequence[str]) -> Embeddings:
        self.query_batches.append(list(texts))
        return _vectors(texts)

    @property
    def passages_embedded(self) -> int:
        return sum(len(batch) for batch in self.passage_batches)


def _vectors(texts: Sequence[str]) -> Embeddings:
    rows = []
    for text in texts:
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        vector = np.random.default_rng(seed).normal(size=EMBEDDING_DIMENSIONS)
        rows.append(vector / np.linalg.norm(vector))
    return np.array(rows, dtype=np.float32)


@dataclass
class StoredDocument:
    document: ChunkedDocument
    indexed_at: datetime


@dataclass
class InMemoryChunkStore:
    """`ChunkStore` with the semantics of the Postgres one: an exclusive lock, one swap per call."""

    documents: dict[str, StoredDocument] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def read_hashes(self) -> dict[str, str]:
        return {doc_id: stored.document.document_hash for doc_id, stored in self.documents.items()}

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[ChunkTransaction]:
        async with self._lock:
            self.events.append("lock")
            yield _InMemoryTransaction(self)

    @property
    def chunk_ids(self) -> list[str]:
        return sorted(
            chunk.chunk_id for stored in self.documents.values() for chunk in stored.document.chunks
        )


class _InMemoryTransaction:
    def __init__(self, store: InMemoryChunkStore) -> None:
        self._store = store

    async def read_hashes(self) -> dict[str, str]:
        return await self._store.read_hashes()

    async def replace_document(
        self, document: ChunkedDocument, embeddings: Embeddings, indexed_at: datetime
    ) -> None:
        assert len(embeddings) == len(document.chunks)
        self._store.documents[document.document_id] = StoredDocument(document, indexed_at)
        self._store.replaced.append(document.document_id)

    async def delete_documents(self, document_ids: Iterable[str]) -> None:
        for document_id in document_ids:
            del self._store.documents[document_id]
            self._store.deleted.append(document_id)


@dataclass
class ScriptedSearcher:
    """`ChunkSearcher` that answers a fixed `RankedChunks` and remembers what it was asked."""

    answer: RankedChunks
    calls: list[tuple[str, bool, SearchMode]] = field(default_factory=list)

    async def search(self, query: str, vector: Embeddings | None, mode: SearchMode) -> RankedChunks:
        self.calls.append((query, vector is not None, mode))
        return self.answer


def hit(
    chunk_id: str,
    *,
    section: str | None = None,
    lexical: int | None = None,
    semantic: int | None = None,
) -> SearchHit:
    document_id, slug = chunk_id.split("#")
    section = section or slug
    return SearchHit(
        chunk_id=chunk_id,
        document_id=document_id,
        section=section,
        text=f"text of {chunk_id}",
        lexical_rank=lexical,
        semantic_rank=semantic,
        similarity=0.9 if semantic is not None else None,
    )
