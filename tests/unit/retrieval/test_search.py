"""The query rules, the confidence rule and the events of a search, on doubles of the ports."""

from typing import get_args

import pytest
from structlog.testing import capture_logs
from support.retrieval_doubles import FakeEmbedder, ScriptedSearcher, hit

from deliverius.config import ReliabilityRuleSetting
from deliverius.retrieval.repository import search_parameters, search_statement
from deliverius.retrieval.search import (
    MAX_QUERY_CHARS,
    PolicySearch,
    RankedChunks,
    ReliabilityRule,
    SearchMode,
    is_reliable,
    prepare_query,
)
from deliverius.shared.errors import InvalidInputError, InvariantError

THRESHOLD = 0.85
BOTH = RankedChunks(
    (hit("POL-03#prazo", lexical=1, semantic=1), hit("POL-04#prazo", semantic=2)),
    best_similarity=0.9,
    in_both_lists=True,
)


def make_search(
    answer: RankedChunks = BOTH, rule: ReliabilityRule = ReliabilityRule.BOTH_LISTS_AND_SIMILARITY
) -> tuple[PolicySearch, FakeEmbedder, ScriptedSearcher]:
    embedder, searcher = FakeEmbedder(), ScriptedSearcher(answer)
    return PolicySearch(embedder, searcher, THRESHOLD, rule), embedder, searcher


@pytest.mark.parametrize(
    ("in_both", "similarity", "reliable"),
    [
        (True, 0.90, True),
        (True, THRESHOLD, True),
        (True, 0.8499, False),
        (False, 0.99, False),
        (True, None, False),
        (False, None, False),
    ],
)
def test_the_two_lists_rule_needs_both_lists_and_enough_similarity(
    in_both: bool, similarity: float | None, reliable: bool
) -> None:
    rule = ReliabilityRule.BOTH_LISTS_AND_SIMILARITY

    assert is_reliable(rule, in_both, similarity, THRESHOLD) is reliable


@pytest.mark.parametrize(
    ("in_both", "similarity", "reliable"),
    [
        (True, 0.90, True),
        (False, 0.90, True),
        (False, THRESHOLD, True),
        (True, 0.8499, False),
        (False, 0.8499, False),
        (True, None, False),
        (False, None, False),
    ],
)
def test_the_similarity_only_rule_ignores_the_lists(
    in_both: bool, similarity: float | None, reliable: bool
) -> None:
    rule = ReliabilityRule.SIMILARITY_ONLY

    assert is_reliable(rule, in_both, similarity, THRESHOLD) is reliable


def test_the_rule_values_are_the_ones_the_settings_accept() -> None:
    accepted = set(get_args(ReliabilityRuleSetting.__value__))

    assert {rule.value for rule in ReliabilityRule} == accepted


async def test_a_hybrid_answer_in_both_lists_and_close_enough_is_reliable() -> None:
    search, _, _ = make_search()

    result = await search.search("quanto tempo para reclamar")

    assert result.reliable
    assert [h.chunk_id for h in result.chunks] == ["POL-03#prazo", "POL-04#prazo"]
    assert result.best_similarity == 0.9


async def test_an_answer_in_one_list_is_reliable_when_the_rule_is_similarity_only() -> None:
    answer = RankedChunks((hit("POL-03#prazo", semantic=1),), 0.99, in_both_lists=False)
    search, _, _ = make_search(answer, ReliabilityRule.SIMILARITY_ONLY)

    assert (await search.search("pergunta")).reliable


async def test_an_answer_below_the_threshold_is_not_reliable_under_any_rule() -> None:
    answer = RankedChunks(BOTH.hits, best_similarity=0.80, in_both_lists=True)

    for rule in ReliabilityRule:
        search, _, _ = make_search(answer, rule)
        assert not (await search.search("pergunta")).reliable


async def test_an_answer_without_a_chunk_in_both_lists_is_not_reliable() -> None:
    answer = RankedChunks((hit("POL-03#prazo", semantic=1),), 0.99, in_both_lists=False)
    search, _, _ = make_search(answer)

    result = await search.search("pergunta")

    assert not result.reliable
    assert result.chunks == answer.hits


async def test_an_answer_below_the_threshold_is_not_reliable() -> None:
    answer = RankedChunks(BOTH.hits, best_similarity=0.80, in_both_lists=True)
    search, _, _ = make_search(answer)

    assert not (await search.search("pergunta")).reliable


@pytest.mark.parametrize("mode", [SearchMode.LEXICAL, SearchMode.SEMANTIC])
async def test_only_the_hybrid_mode_judges_confidence(mode: SearchMode) -> None:
    search, _, _ = make_search()

    result = await search.search("pergunta", mode)

    assert not result.reliable
    assert result.chunks == BOTH.hits


async def test_the_lexical_mode_does_not_embed_the_query() -> None:
    search, embedder, searcher = make_search()

    await search.search("pergunta", SearchMode.LEXICAL)

    assert embedder.query_batches == []
    assert searcher.calls == [("pergunta", False, SearchMode.LEXICAL)]


@pytest.mark.parametrize("mode", [SearchMode.SEMANTIC, SearchMode.HYBRID])
async def test_the_other_modes_embed_the_normalized_query_once(mode: SearchMode) -> None:
    search, embedder, searcher = make_search()

    await search.search("pergunta", mode)

    assert embedder.query_batches == [["pergunta"]]
    assert searcher.calls == [("pergunta", True, mode)]


async def test_the_query_reaches_the_searcher_normalized() -> None:
    search, _, searcher = make_search()

    await search.search("  \uff50\uff45\uff44\uff49\uff44\uff4f\u200b   atrasado  ")

    assert searcher.calls[0][0] == "pedido atrasado"


async def test_the_default_mode_is_hybrid() -> None:
    search, _, searcher = make_search()

    await search.search("pergunta")

    assert searcher.calls[0][2] is SearchMode.HYBRID


def test_a_query_at_the_limit_is_accepted() -> None:
    assert len(prepare_query("a" * MAX_QUERY_CHARS)) == MAX_QUERY_CHARS


@pytest.mark.parametrize("query", ["a" * (MAX_QUERY_CHARS + 1), "ﬃ" * (MAX_QUERY_CHARS // 2 + 1)])
async def test_a_long_query_is_refused_before_anything_is_embedded_or_searched(query: str) -> None:
    search, embedder, searcher = make_search()

    with pytest.raises(InvalidInputError, match=r"search\.query_too_long"):
        await search.search(query)

    assert embedder.query_batches == []
    assert searcher.calls == []


@pytest.mark.parametrize("query", ["", "   ", "\u200b\u200b"])
async def test_an_empty_query_is_refused(query: str) -> None:
    search, embedder, searcher = make_search()

    with pytest.raises(InvalidInputError, match=r"search\.query_empty"):
        await search.search(query)

    assert embedder.query_batches == []
    assert searcher.calls == []


async def test_the_search_event_has_the_ids_and_never_the_text() -> None:
    search, _, _ = make_search()
    unique_text = "consulta-unica-4f9c1e7a"

    with capture_logs() as logs:
        await search.search(unique_text)

    [event] = [log for log in logs if log["event"] == "retrieval.search"]
    assert event["mode"] == "hybrid"
    assert event["chunk_ids"] == ["POL-03#prazo", "POL-04#prazo"]
    assert event["chunk_count"] == 2
    assert event["reliable"] is True
    assert event["latency_ms"] >= 0
    assert unique_text not in repr(logs)


async def test_an_unreliable_search_is_logged_apart_and_never_with_the_text() -> None:
    answer = RankedChunks((hit("POL-10#prazo", semantic=1),), 0.78, in_both_lists=False)
    search, _, _ = make_search(answer)
    unique_text = "outra-consulta-unica-8b2d"

    with capture_logs() as logs:
        await search.search(unique_text)

    [event] = [log for log in logs if log["event"] == "retrieval.search_unreliable"]
    assert event["in_both_lists"] is False
    assert event["best_similarity"] == 0.78
    assert unique_text not in repr(logs)


async def test_a_reliable_search_does_not_log_the_unreliable_event() -> None:
    search, _, _ = make_search()

    with capture_logs() as logs:
        await search.search("pergunta")

    assert "retrieval.search_unreliable" not in {log["event"] for log in logs}


async def test_a_refused_query_is_not_logged_either() -> None:
    search, _, _ = make_search()
    unique_text = "x" * MAX_QUERY_CHARS + "consulta-longa-unica-77"

    with capture_logs() as logs, pytest.raises(InvalidInputError):
        await search.search(unique_text)

    assert "consulta-longa-unica-77" not in repr(logs)


def test_the_lexical_statement_binds_only_what_it_uses() -> None:
    parameters = search_parameters("pergunta", None, SearchMode.LEXICAL)

    assert parameters == {"list_size": 5, "result_size": 3, "query": "pergunta"}
    assert ":vector" not in str(search_statement(SearchMode.LEXICAL))


def test_the_semantic_statement_needs_the_vector_and_not_the_query() -> None:
    vector = FakeEmbedder().embed_queries(["pergunta"])

    parameters = search_parameters("pergunta", vector, SearchMode.SEMANTIC)

    assert set(parameters) == {"list_size", "result_size", "vector"}


def test_the_hybrid_statement_uses_the_fusion_constant() -> None:
    vector = FakeEmbedder().embed_queries(["pergunta"])

    parameters = search_parameters("pergunta", vector, SearchMode.HYBRID)

    assert parameters["rrf_k"] == 60
    assert parameters["query"] == "pergunta"


def test_the_query_is_never_part_of_the_sql_text() -> None:
    hostile = "'; DROP TABLE policy_chunks; --"
    vector = FakeEmbedder().embed_queries([hostile])

    for mode in SearchMode:
        assert hostile not in str(search_statement(mode))
        assert search_parameters(hostile, vector, mode).get("query", hostile) == hostile


def test_the_semantic_modes_refuse_a_missing_vector() -> None:
    with pytest.raises(InvariantError, match=r"search\.vector_missing"):
        search_parameters("pergunta", None, SearchMode.HYBRID)
