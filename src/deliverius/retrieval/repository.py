"""`policy_chunks` on Postgres: stored hashes, the swap of a document's chunks, and the search SQL.

Policies are public to every caller, like the restaurant catalog: no sandbox, no customer, so no
method here takes an `AccessScope`. The table holds the same dozen documents for everyone, and
nothing in it is personal data (`docs/autorizacao.md`).
"""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Final

from pgvector.sqlalchemy import Vector
from sqlalchemy import BindParameter, TextClause, bindparam, delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deliverius.db.errors import translate_db_errors
from deliverius.db.tables import PolicyChunkRow
from deliverius.retrieval.chunking import ChunkedDocument
from deliverius.retrieval.embedder import EMBEDDING_DIMENSIONS, Embeddings
from deliverius.retrieval.indexer import ChunkTransaction
from deliverius.retrieval.search import (
    LIST_SIZE,
    RESULT_SIZE,
    RRF_K,
    RankedChunks,
    SearchHit,
    SearchMode,
)
from deliverius.shared.errors import InvariantError

# Any constant works, as long as no other part of the application takes the same advisory lock.
INDEX_LOCK_KEY: Final = 7_204_103_301

# Any word may match (OR), because a question rarely repeats every word of a section. The terms
# come from `plainto_tsquery`, which reads no operator the user typed (`!`, `|`, `:*`, quotes),
# and the AND between them is turned into OR in the text of the query. A query of only stop
# words gives an empty tsquery, which matches nothing.
_PARSED = """
parsed AS (
  SELECT replace(plainto_tsquery('pt_unaccent', :query)::text, '&', '|')::tsquery AS query)"""
_LEXICAL = """
lexical AS (
  SELECT chunk_id,
         row_number() OVER (ORDER BY ts_rank_cd(tsv, parsed.query) DESC, chunk_id) AS rank
  FROM policy_chunks, parsed
  WHERE tsv @@ parsed.query
  ORDER BY rank
  LIMIT :list_size)"""
_SEMANTIC = """
semantic AS (
  SELECT chunk_id,
         1 - (embedding <=> :vector) AS similarity,
         row_number() OVER (ORDER BY embedding <=> :vector, chunk_id) AS rank
  FROM policy_chunks
  ORDER BY rank
  LIMIT :list_size)"""
# The f-strings below join constant fragments only: the query and the vector are bind parameters.
_COLUMNS = "c.chunk_id, c.document_id, c.section, c.content"
_HYBRID_SQL = f"""
WITH {_PARSED},
{_LEXICAL},
{_SEMANTIC}
SELECT {_COLUMNS},
       l.rank AS lexical_rank, s.rank AS semantic_rank, s.similarity,
       max(s.similarity) OVER () AS best_similarity,
       coalesce(bool_or(l.rank IS NOT NULL AND s.rank IS NOT NULL) OVER (), false) AS in_both_lists
FROM lexical l
FULL JOIN semantic s USING (chunk_id)
JOIN policy_chunks c USING (chunk_id)
ORDER BY coalesce(1.0 / (:rrf_k + l.rank), 0) + coalesce(1.0 / (:rrf_k + s.rank), 0) DESC,
         c.chunk_id
LIMIT :result_size"""  # noqa: S608
_LEXICAL_SQL = f"""
WITH {_PARSED},
{_LEXICAL}
SELECT {_COLUMNS},
       l.rank AS lexical_rank, NULL::bigint AS semantic_rank, NULL::float8 AS similarity,
       NULL::float8 AS best_similarity, false AS in_both_lists
FROM lexical l
JOIN policy_chunks c USING (chunk_id)
ORDER BY l.rank
LIMIT :result_size"""  # noqa: S608
_SEMANTIC_SQL = f"""
WITH {_SEMANTIC}
SELECT {_COLUMNS},
       NULL::bigint AS lexical_rank, s.rank AS semantic_rank, s.similarity,
       max(s.similarity) OVER () AS best_similarity, false AS in_both_lists
FROM semantic s
JOIN policy_chunks c USING (chunk_id)
ORDER BY s.rank
LIMIT :result_size"""  # noqa: S608


def search_statement(mode: SearchMode, *, explain: bool = False) -> TextClause:
    """The SQL of a mode. `explain` prefixes `EXPLAIN`, so a test reads the plan of what runs."""
    prefix = "EXPLAIN " if explain else ""
    match mode:
        case SearchMode.LEXICAL:
            return text(prefix + _LEXICAL_SQL)
        case SearchMode.SEMANTIC:
            return text(prefix + _SEMANTIC_SQL).bindparams(_vector_parameter())
        case SearchMode.HYBRID:
            return text(prefix + _HYBRID_SQL).bindparams(_vector_parameter())


def search_parameters(query: str, vector: Embeddings | None, mode: SearchMode) -> dict[str, object]:
    parameters: dict[str, object] = {"list_size": LIST_SIZE, "result_size": RESULT_SIZE}
    if mode is not SearchMode.SEMANTIC:
        parameters["query"] = query
    if mode is not SearchMode.LEXICAL:
        if vector is None:
            raise InvariantError("search.vector_missing")
        parameters["vector"] = vector[0]
    if mode is SearchMode.HYBRID:
        parameters["rrf_k"] = RRF_K
    return parameters


def _vector_parameter() -> BindParameter[Any]:
    return bindparam("vector", type_=Vector(EMBEDDING_DIMENSIONS))


class PostgresPolicyChunkRepository:
    """`ChunkStore` and `ChunkSearcher` on Postgres. Each call opens its own short session."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], lock_timeout_ms: int
    ) -> None:
        self._session_factory = session_factory
        self._lock_timeout_ms = lock_timeout_ms

    async def read_hashes(self) -> dict[str, str]:
        with translate_db_errors():
            async with self._session_factory() as session:
                return await _read_hashes(session)

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[ChunkTransaction]:
        """One transaction that waits (up to the lock timeout) for the indexing lock."""
        with translate_db_errors():
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('lock_timeout', :milliseconds, true)"),
                    {"milliseconds": f"{self._lock_timeout_ms}ms"},
                )
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": INDEX_LOCK_KEY}
                )
                yield _PostgresChunkTransaction(session)

    async def search(self, query: str, vector: Embeddings | None, mode: SearchMode) -> RankedChunks:
        statement = search_statement(mode)
        with translate_db_errors():
            async with self._session_factory() as session:
                await session.execute(text("SET TRANSACTION READ ONLY"))
                rows = (
                    await session.execute(statement, search_parameters(query, vector, mode))
                ).all()
        hits = tuple(
            SearchHit(
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                section=row.section,
                text=row.content,
                lexical_rank=row.lexical_rank,
                semantic_rank=row.semantic_rank,
                similarity=row.similarity,
            )
            for row in rows
        )
        if not rows:
            return RankedChunks(hits, best_similarity=None, in_both_lists=False)
        return RankedChunks(hits, rows[0].best_similarity, bool(rows[0].in_both_lists))


class _PostgresChunkTransaction:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def read_hashes(self) -> dict[str, str]:
        return await _read_hashes(self._session)

    async def replace_document(
        self, document: ChunkedDocument, embeddings: Embeddings, indexed_at: datetime
    ) -> None:
        await self._session.execute(
            delete(PolicyChunkRow).where(PolicyChunkRow.document_id == document.document_id)
        )
        await self._session.execute(
            insert(PolicyChunkRow),
            [
                {
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "section": chunk.section,
                    "position": chunk.position,
                    "content": chunk.content,
                    "document_hash": document.document_hash,
                    "embedding": embeddings[index],
                    "indexed_at": indexed_at,
                }
                for index, chunk in enumerate(document.chunks)
            ],
        )

    async def delete_documents(self, document_ids: Iterable[str]) -> None:
        await self._session.execute(
            delete(PolicyChunkRow).where(PolicyChunkRow.document_id.in_(list(document_ids)))
        )


async def _read_hashes(session: AsyncSession) -> dict[str, str]:
    statement = select(PolicyChunkRow.document_id, PolicyChunkRow.document_hash).distinct()
    return {row.document_id: row.document_hash for row in (await session.execute(statement)).all()}
