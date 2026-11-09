"""The search on Postgres with the real model and the real policies."""

from collections.abc import AsyncIterator, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from support.database import settings_for
from support.retrieval_database import search_for

from deliverius.config import Settings
from deliverius.db.engine import create_engine
from deliverius.retrieval import E5Embedder, PolicySearch, ReliabilityRule, SearchMode
from deliverius.retrieval.repository import search_parameters, search_statement

pytestmark = pytest.mark.integration

POLICY_QUESTION = "quanto tempo tenho pra reclamar de item faltando"
OFF_TOPIC_QUESTION = "qual a previsão do tempo amanhã"
# The measured default (docs/busca-politicas.md), not a value chosen for the test.
DEFAULT_MIN_SIMILARITY: float = Settings.model_fields["retrieval_min_similarity"].default
HOSTILE_QUERIES = [
    "!",
    "&",
    "|",
    ":*",
    '"aberta',
    "pedido & !atrasado | cancelar:*",
    "'; DROP TABLE policy_chunks; --",
    "a <-> b",
    "(((",
    "\\",
    "cancelar -pedido",
    "de a o que",
    "'",
]


@pytest.fixture
async def search_engine(indexed_policies: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings_for(indexed_policies))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def search(search_engine: AsyncEngine, embedder: E5Embedder) -> PolicySearch:
    return search_for(search_engine, embedder)


async def test_a_query_without_accents_finds_the_accented_text(search: PolicySearch) -> None:
    result = await search.search("cobranca duplicada", SearchMode.LEXICAL)

    assert result.chunks
    assert any("cobrança" in hit.text.casefold() for hit in result.chunks)
    assert all(hit.lexical_rank is not None for hit in result.chunks)


@pytest.mark.parametrize("query", HOSTILE_QUERIES)
@pytest.mark.parametrize("mode", list(SearchMode))
async def test_operators_in_the_query_never_cause_an_error(
    search: PolicySearch, query: str, mode: SearchMode
) -> None:
    result = await search.search(query, mode)

    assert len(result.chunks) <= 3


@pytest.mark.parametrize("mode", list(SearchMode))
async def test_the_same_query_gives_the_same_answer(search: PolicySearch, mode: SearchMode) -> None:
    first = await search.search("cancelar pedido", mode)
    second = await search.search("cancelar pedido", mode)

    assert first == second
    assert first.chunks


async def test_a_question_of_the_policy_brings_its_section_among_the_first_three(
    search: PolicySearch,
) -> None:
    result = await search.search(POLICY_QUESTION)

    assert "POL-03#prazo" in [hit.chunk_id for hit in result.chunks]
    assert len(result.chunks) == 3


@pytest.mark.parametrize("rule", list(ReliabilityRule))
async def test_a_question_of_the_policy_is_reliable_under_both_rules(
    search_engine: AsyncEngine, embedder: E5Embedder, rule: ReliabilityRule
) -> None:
    search = search_for(search_engine, embedder, min_similarity=DEFAULT_MIN_SIMILARITY, rule=rule)

    result = await search.search(POLICY_QUESTION)

    assert result.reliable, (
        f"in_both_lists={result.in_both_lists} best_similarity={result.best_similarity}"
    )
    assert result.in_both_lists


async def test_any_word_of_the_query_may_match_the_lexical_list(search: PolicySearch) -> None:
    result = await search.search("pedido zzzxyzabc qwertyuiop", SearchMode.LEXICAL)

    assert result.chunks
    assert all("pedid" in hit.text.casefold() for hit in result.chunks)


async def test_a_query_of_only_stop_words_gives_an_empty_lexical_list(search: PolicySearch) -> None:
    result = await search.search("de a o que", SearchMode.LEXICAL)

    assert result.chunks == ()
    assert not result.in_both_lists


async def test_a_query_of_only_stop_words_still_has_a_semantic_answer(search: PolicySearch) -> None:
    result = await search.search("de a o que", SearchMode.HYBRID)

    assert len(result.chunks) == 3
    assert not result.in_both_lists


@pytest.mark.parametrize("operator_query", ["!pedido", "pedido:*", '"pedido', "-pedido", "pedido|"])
async def test_an_operator_typed_by_the_user_does_not_change_the_lexical_list(
    search: PolicySearch, operator_query: str
) -> None:
    plain = await search.search("pedido", SearchMode.LEXICAL)

    result = await search.search(operator_query, SearchMode.LEXICAL)

    assert [hit.chunk_id for hit in result.chunks] == [hit.chunk_id for hit in plain.chunks]


async def test_the_hybrid_hits_carry_the_position_in_each_list(search: PolicySearch) -> None:
    result = await search.search("pedido não chegou reembolso")

    for hit in result.chunks:
        assert hit.lexical_rank is not None or hit.semantic_rank is not None
        assert (hit.similarity is not None) == (hit.semantic_rank is not None)
    assert all(
        rank is None or 1 <= rank <= 5
        for hit in result.chunks
        for rank in (hit.lexical_rank, hit.semantic_rank)
    )


async def test_the_semantic_mode_returns_chunks_by_decreasing_similarity(
    search: PolicySearch,
) -> None:
    result = await search.search(POLICY_QUESTION, SearchMode.SEMANTIC)

    similarities = [hit.similarity for hit in result.chunks]
    assert similarities == sorted(similarities, reverse=True)  # type: ignore[type-var]
    assert [hit.semantic_rank for hit in result.chunks] == [1, 2, 3]


async def test_the_plan_of_the_hybrid_search_reads_the_gin_index_and_limits_both_lists(
    indexed_policies: str, embedder: E5Embedder, record_property: Callable[[str, object], None]
) -> None:
    engine = create_engine(settings_for(indexed_policies))
    statement = search_statement(SearchMode.HYBRID, explain=True)
    vector = embedder.embed_queries([POLICY_QUESTION])
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET LOCAL enable_seqscan = off"))
            plan = await connection.execute(
                statement, search_parameters("reclamar item faltando", vector, SearchMode.HYBRID)
            )
            lines = "\n".join(plan.scalars().all())
    finally:
        await engine.dispose()

    record_property("explain_hybrid", lines)
    assert "ix_policy_chunks_tsv" in lines
    assert lines.count("Limit") >= 3
