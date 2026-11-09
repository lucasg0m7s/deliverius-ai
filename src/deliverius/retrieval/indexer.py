"""Incremental indexing of the policy documents, safe against two indexers at the same time.

The embeddings (hundreds of milliseconds of ONNX) are computed before any transaction opens. The
transaction that swaps the chunks is short and holds an advisory lock; inside it the stored hashes
are read again, so an indexer that lost the race finds the work done and writes nothing.
"""

import asyncio
from collections.abc import Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import structlog

from deliverius.policy import POLICY_LIMITS, PolicyDocument, PolicyLimits
from deliverius.retrieval.chunking import ChunkedDocument, chunk_policy_documents
from deliverius.retrieval.embedder import Embeddings
from deliverius.shared.clock import Clock, SystemClock

logger = structlog.get_logger(__name__)


class PassageEmbedder(Protocol):
    def embed_passages(self, texts: Sequence[str]) -> Embeddings: ...


class ChunkTransaction(Protocol):
    """The writes of one exclusive transaction on the chunks."""

    async def read_hashes(self) -> dict[str, str]:
        """`document_id -> document_hash` as stored right now."""
        ...

    async def replace_document(
        self, document: ChunkedDocument, embeddings: Embeddings, indexed_at: datetime
    ) -> None:
        """Delete every chunk of the document and insert the new ones."""
        ...

    async def delete_documents(self, document_ids: Iterable[str]) -> None: ...


class ChunkStore(Protocol):
    async def read_hashes(self) -> dict[str, str]: ...

    def exclusive(self) -> AbstractAsyncContextManager[ChunkTransaction]:
        """A transaction that excludes every other indexer until it ends."""
        ...


@dataclass(frozen=True, slots=True)
class IndexReport:
    recalculated: tuple[str, ...]
    kept: tuple[str, ...]
    removed: tuple[str, ...]


class PolicyIndexer:
    def __init__(
        self,
        store: ChunkStore,
        embedder: PassageEmbedder,
        clock: Clock | None = None,
        documents: Sequence[PolicyDocument] | None = None,
        limits: PolicyLimits = POLICY_LIMITS,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._clock = clock or SystemClock()
        self._documents = documents
        self._limits = limits

    async def reindex(self) -> IndexReport:
        """Recompute only the documents whose hash changed; drop the ones that no longer exist."""
        chunked = chunk_policy_documents(self._documents, self._limits)
        current_ids = {document.document_id for document in chunked}
        stored = await self._store.read_hashes()
        changed = [doc for doc in chunked if stored.get(doc.document_id) != doc.document_hash]
        if not changed and stored.keys() <= current_ids:
            return self._report(chunked, written=(), removed=())
        embeddings = await self._embed(changed)
        written, removed = await self._swap(chunked, changed, embeddings)
        return self._report(chunked, written, removed)

    async def _embed(self, changed: Sequence[ChunkedDocument]) -> dict[str, Embeddings]:
        texts = [chunk.content for document in changed for chunk in document.chunks]
        if not texts:
            return {}
        # Off the event loop: the session is CPU-bound and would stall every other request.
        vectors = await asyncio.to_thread(self._embedder.embed_passages, texts)
        by_document: dict[str, Embeddings] = {}
        start = 0
        for document in changed:
            end = start + len(document.chunks)
            by_document[document.document_id] = vectors[start:end]
            start = end
        return by_document

    async def _swap(
        self,
        chunked: Sequence[ChunkedDocument],
        changed: Sequence[ChunkedDocument],
        embeddings: dict[str, Embeddings],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        async with self._store.exclusive() as transaction:
            stored = await transaction.read_hashes()
            written = []
            for document in changed:
                if stored.get(document.document_id) == document.document_hash:
                    continue
                await transaction.replace_document(
                    document, embeddings[document.document_id], self._clock.now()
                )
                written.append(document.document_id)
            current_ids = {document.document_id for document in chunked}
            removed = sorted(stored.keys() - current_ids)
            if removed:
                await transaction.delete_documents(removed)
        return tuple(written), tuple(removed)

    @staticmethod
    def _report(
        chunked: Sequence[ChunkedDocument], written: tuple[str, ...], removed: tuple[str, ...]
    ) -> IndexReport:
        kept = tuple(doc.document_id for doc in chunked if doc.document_id not in written)
        report = IndexReport(recalculated=written, kept=kept, removed=removed)
        logger.info(
            "retrieval.indexed",
            recalculated=list(report.recalculated),
            kept_count=len(report.kept),
            removed=list(report.removed),
        )
        return report
