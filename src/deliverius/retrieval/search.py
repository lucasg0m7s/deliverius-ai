"""Search over the policy chunks: by word, by meaning, or both fused with Reciprocal Rank Fusion.

The fusion itself runs in SQL (`repository.py`). This module normalises and bounds the query, embeds
it, asks the repository, and applies the rule that tells the agent when the answer cannot be
trusted. The text of the query is never logged: it may carry what a customer wrote.
"""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

import structlog

from deliverius.privacy import normalize_text
from deliverius.retrieval.embedder import Embeddings
from deliverius.shared.errors import InvalidInputError

logger = structlog.get_logger(__name__)

MAX_QUERY_CHARS: Final = 500
LIST_SIZE: Final = 5
RESULT_SIZE: Final = 3
RRF_K: Final = 60
_MS_PER_SECOND: Final = 1_000


class ReliabilityRule(StrEnum):
    """When the hybrid result can be trusted; the choice comes from the evaluation."""

    # Some chunk is in both lists and the best similarity reaches the threshold.
    BOTH_LISTS_AND_SIMILARITY = "both_lists_and_similarity"
    # The best similarity reaches the threshold.
    SIMILARITY_ONLY = "similarity_only"


class SearchMode(StrEnum):
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A chunk of the answer. A rank is the position in that list from 1, `None` if it is absent."""

    chunk_id: str
    document_id: str
    section: str
    text: str
    lexical_rank: int | None
    semantic_rank: int | None
    # Cosine similarity with the query; `None` when the chunk is not in the semantic list.
    similarity: float | None


@dataclass(frozen=True, slots=True)
class RankedChunks:
    """What the repository returns: the hits plus the two facts the confidence rule needs."""

    hits: tuple[SearchHit, ...]
    # The best similarity of the whole semantic list, which may be outside the hits.
    best_similarity: float | None
    # Whether some chunk is in both lists, counting beyond the hits.
    in_both_lists: bool


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunks: tuple[SearchHit, ...]
    # Only the hybrid mode judges confidence; the other two are for the evaluation and say `False`.
    reliable: bool
    best_similarity: float | None
    in_both_lists: bool


class QueryEmbedder(Protocol):
    def embed_queries(self, texts: Sequence[str]) -> Embeddings: ...


class ChunkSearcher(Protocol):
    async def search(self, query: str, vector: Embeddings | None, mode: SearchMode) -> RankedChunks:
        """`vector` is the embedding of the query: absent only in the lexical mode."""
        ...


def is_reliable(
    rule: ReliabilityRule,
    in_both_lists: bool,
    best_similarity: float | None,
    min_similarity: float,
) -> bool:
    """The closest chunk is close enough in meaning, and (by rule) both searches agree."""
    if best_similarity is None or best_similarity < min_similarity:
        return False
    return in_both_lists or rule is ReliabilityRule.SIMILARITY_ONLY


def prepare_query(query: str) -> str:
    """Normalised and bounded; the length is checked before and after, since NFKC can expand."""
    if len(query) > MAX_QUERY_CHARS:
        raise InvalidInputError("search.query_too_long")
    normalized = normalize_text(query).strip()
    if len(normalized) > MAX_QUERY_CHARS:
        raise InvalidInputError("search.query_too_long")
    if not normalized:
        raise InvalidInputError("search.query_empty")
    return normalized


class PolicySearch:
    def __init__(
        self,
        embedder: QueryEmbedder,
        searcher: ChunkSearcher,
        min_similarity: float,
        rule: ReliabilityRule,
    ) -> None:
        self._embedder = embedder
        self._searcher = searcher
        self._min_similarity = min_similarity
        self._rule = rule

    async def search(self, query: str, mode: SearchMode = SearchMode.HYBRID) -> SearchResult:
        """The best three chunks. Raises `InvalidInputError` for an empty or over-long query."""
        normalized = prepare_query(query)
        started = time.perf_counter()
        vector = await self._embed(normalized, mode)
        ranked = await self._searcher.search(normalized, vector, mode)
        result = self._judge(ranked, mode)
        self._log(mode, result, time.perf_counter() - started)
        return result

    async def _embed(self, query: str, mode: SearchMode) -> Embeddings | None:
        if mode is SearchMode.LEXICAL:
            return None
        # Off the event loop: the ONNX session is CPU-bound.
        vectors = await asyncio.to_thread(self._embedder.embed_queries, [query])
        return vectors[:1]

    def _judge(self, ranked: RankedChunks, mode: SearchMode) -> SearchResult:
        reliable = mode is SearchMode.HYBRID and is_reliable(
            self._rule, ranked.in_both_lists, ranked.best_similarity, self._min_similarity
        )
        return SearchResult(ranked.hits, reliable, ranked.best_similarity, ranked.in_both_lists)

    @staticmethod
    def _log(mode: SearchMode, result: SearchResult, seconds: float) -> None:
        chunk_ids = [hit.chunk_id for hit in result.chunks]
        logger.info(
            "retrieval.search",
            mode=mode.value,
            latency_ms=round(seconds * _MS_PER_SECOND, 1),
            chunk_count=len(chunk_ids),
            chunk_ids=chunk_ids,
            reliable=result.reliable,
        )
        if mode is SearchMode.HYBRID and not result.reliable:
            logger.info(
                "retrieval.search_unreliable",
                in_both_lists=result.in_both_lists,
                best_similarity=result.best_similarity,
            )
