"""The metrics, worked by hand, and the evaluation on a searcher that answers from a table."""

import json
from pathlib import Path

import pytest
from support.retrieval_doubles import hit

from deliverius.retrieval.evaluation import (
    CANDIDATE_THRESHOLDS,
    EvaluationDataError,
    QaPair,
    RuleOutcome,
    bootstrap_interval,
    distribution,
    evaluate,
    evaluate_confidence,
    evaluate_mode,
    load_out_of_scope,
    load_pairs,
    measure_latency,
    percentile,
    reciprocal_rank,
)
from deliverius.retrieval.search import SearchHit, SearchMode, SearchResult
from deliverius.shared.errors import InvariantError

PAIR = QaPair("quanto tempo tenho?", "POL-03", "Prazo")


def chunk(document: str, section: str) -> SearchHit:
    return hit(f"{document}#x", section=section)


class TableSearcher:
    """Answers each question with the result registered for it, per mode when given."""

    def __init__(self, results: dict[str, SearchResult]) -> None:
        self._results = results
        self.asked: list[tuple[str, SearchMode]] = []

    async def search(self, query: str, mode: SearchMode) -> SearchResult:
        self.asked.append((query, mode))
        return self._results[query]


def result(
    *hits: SearchHit, similarity: float | None = 0.9, both: bool = True, reliable: bool = True
) -> SearchResult:
    return SearchResult(hits, reliable, similarity, both)


@pytest.mark.parametrize(
    ("hits", "expected"),
    [
        ([chunk("POL-03", "Prazo")], 1.0),
        ([chunk("POL-01", "Prazo"), chunk("POL-03", "Prazo")], 0.5),
        ([chunk("POL-01", "A"), chunk("POL-02", "B"), chunk("POL-03", "Prazo")], 1 / 3),
        ([chunk("POL-01", "A"), chunk("POL-02", "B"), chunk("POL-04", "C")], 0.0),
        ([], 0.0),
        # Same section title in another document, same document and another section: both misses.
        ([chunk("POL-04", "Prazo"), chunk("POL-03", "Quando se aplica")], 0.0),
    ],
)
def test_the_reciprocal_rank_is_one_over_the_position_of_the_first_hit(
    hits: list[SearchHit], expected: float
) -> None:
    assert reciprocal_rank(hits, PAIR) == pytest.approx(expected)


def test_a_hit_beyond_the_third_place_does_not_count() -> None:
    hits = [
        chunk("POL-01", "A"),
        chunk("POL-02", "B"),
        chunk("POL-04", "C"),
        chunk("POL-03", "Prazo"),
    ]

    assert reciprocal_rank(hits, PAIR) == 0.0


async def test_recall_and_mrr_are_the_means_over_the_pairs() -> None:
    pairs = [QaPair(f"q{n}", "POL-03", "Prazo") for n in range(4)]
    searcher = TableSearcher(
        {
            "q0": result(chunk("POL-03", "Prazo")),
            "q1": result(chunk("POL-01", "A"), chunk("POL-03", "Prazo")),
            "q2": result(chunk("POL-01", "A"), chunk("POL-02", "B"), chunk("POL-03", "Prazo")),
            "q3": result(chunk("POL-01", "A")),
        }
    )

    report = await evaluate_mode(searcher, pairs, SearchMode.SEMANTIC)

    assert report.mode is SearchMode.SEMANTIC
    assert report.pairs == 4
    score = report.by_section
    assert score.recall_at_3 == pytest.approx(0.75)
    assert score.mrr == pytest.approx((1 + 0.5 + 1 / 3 + 0) / 4)
    assert score.recall_interval.low <= score.recall_at_3 <= score.recall_interval.high
    assert score.mrr_interval.low <= score.mrr <= score.mrr_interval.high
    assert {mode for _, mode in searcher.asked} == {SearchMode.SEMANTIC}


async def test_a_chunk_of_the_right_document_counts_by_document_and_not_by_section() -> None:
    pairs = [QaPair(f"q{n}", "POL-03", "Prazo") for n in range(3)]
    searcher = TableSearcher(
        {
            "q0": result(chunk("POL-03", "Quando se aplica")),
            "q1": result(chunk("POL-01", "Prazo"), chunk("POL-03", "O que faz")),
            "q2": result(chunk("POL-01", "Prazo"), chunk("POL-02", "Prazo")),
        }
    )

    report = await evaluate_mode(searcher, pairs, SearchMode.HYBRID)

    assert report.by_section.recall_at_3 == 0.0
    assert report.by_section.mrr == 0.0
    assert report.by_document.recall_at_3 == pytest.approx(2 / 3)
    assert report.by_document.mrr == pytest.approx((1 + 0.5 + 0) / 3)
    low, high = report.by_document.recall_interval.low, report.by_document.recall_interval.high
    assert low <= report.by_document.recall_at_3 <= high


def test_the_document_rank_ignores_the_section() -> None:
    hits = [chunk("POL-01", "Prazo"), chunk("POL-03", "Outra")]

    assert reciprocal_rank(hits, PAIR, by_document=True) == 0.5
    assert reciprocal_rank(hits, PAIR) == 0.0


def test_the_bootstrap_of_identical_values_is_that_value() -> None:
    interval = bootstrap_interval([1.0] * 20)

    assert (interval.low, interval.high) == (1.0, 1.0)


def test_the_bootstrap_is_reproducible_with_the_same_seed() -> None:
    values = [1.0, 0.0, 0.5, 1.0, 0.0, 1 / 3, 1.0, 0.0]

    assert bootstrap_interval(values, seed=7) == bootstrap_interval(values, seed=7)
    assert bootstrap_interval(values, seed=7) != bootstrap_interval(values, seed=8)


def test_the_bootstrap_interval_brackets_the_mean_and_stays_in_range() -> None:
    values = [1.0] * 30 + [0.0] * 30

    interval = bootstrap_interval(values)

    assert 0.35 < interval.low < 0.5 < interval.high < 0.65
    assert 0.0 <= interval.low <= interval.high <= 1.0


def test_a_wider_confidence_gives_a_wider_interval() -> None:
    values = [1.0] * 30 + [0.0] * 30

    narrow = bootstrap_interval(values, confidence=0.5)
    wide = bootstrap_interval(values, confidence=0.99)

    assert wide.low < narrow.low
    assert wide.high > narrow.high


def test_the_bootstrap_of_nothing_is_refused() -> None:
    with pytest.raises(InvariantError, match=r"evaluation\.bootstrap_empty"):
        bootstrap_interval([])


def test_the_distribution_is_minimum_p5_p25_median_maximum() -> None:
    values = [float(n) for n in range(101)]

    spread = distribution(values)

    assert (spread.minimum, spread.p5, spread.p25, spread.median, spread.maximum) == (
        0.0,
        5.0,
        25.0,
        50.0,
        100.0,
    )


def test_the_distribution_of_nothing_is_refused() -> None:
    with pytest.raises(InvariantError, match=r"evaluation\.no_similarity"):
        distribution([])


def test_the_candidate_thresholds_run_from_080_to_090_by_one_hundredth() -> None:
    assert CANDIDATE_THRESHOLDS[0] == 0.80
    assert CANDIDATE_THRESHOLDS[-1] == 0.90
    assert len(CANDIDATE_THRESHOLDS) == 11


async def test_confidence_reports_the_refusals_and_the_threshold_table() -> None:
    pairs = [QaPair(f"p{n}", "POL-03", "Prazo") for n in range(4)]
    searcher = TableSearcher(
        {
            "p0": result(similarity=0.93, both=True, reliable=True),
            "p1": result(similarity=0.88, both=True, reliable=True),
            "p2": result(similarity=0.82, both=True, reliable=False),
            "p3": result(similarity=0.95, both=False, reliable=False),
            "o0": result(similarity=0.84, both=False, reliable=False),
            "o1": result(similarity=0.91, both=True, reliable=True),
        }
    )

    report = await evaluate_confidence(searcher, pairs, ["o0", "o1"])

    assert report.pairs_unreliable_rate == 0.5
    assert report.out_of_scope_unreliable_rate == 0.5
    assert report.pairs_in_both_lists == 0.75
    assert report.out_of_scope_in_both_lists == 0.5
    assert report.pairs_similarity.maximum == 0.95
    assert report.pairs_similarity.minimum == 0.82
    assert report.out_of_scope_similarity.median == pytest.approx(0.875)
    by_threshold = {row.threshold: row for row in report.thresholds}
    assert by_threshold[0.80].both_lists_and_similarity == RuleOutcome(0.75, 0.5)
    assert by_threshold[0.90].both_lists_and_similarity == RuleOutcome(0.25, 0.5)
    assert by_threshold[0.88].both_lists_and_similarity == RuleOutcome(0.5, 0.5)
    assert by_threshold[0.88].similarity_only == RuleOutcome(0.75, 0.5)
    assert by_threshold[0.80].similarity_only == RuleOutcome(1.0, 0.0)
    assert by_threshold[0.90].similarity_only == RuleOutcome(0.5, 0.5)
    assert {mode for _, mode in searcher.asked} == {SearchMode.HYBRID}


async def test_a_missing_similarity_is_left_out_of_the_distribution_and_never_trusted() -> None:
    searcher = TableSearcher(
        {
            "p1": result(similarity=None, both=True, reliable=False),
            "p2": result(similarity=0.9),
            "o": result(similarity=0.8, both=False, reliable=False),
        }
    )
    pairs = [QaPair("p1", "POL-03", "Prazo"), QaPair("p2", "POL-03", "Prazo")]

    report = await evaluate_confidence(searcher, pairs, ["o"])

    assert report.pairs_similarity.minimum == 0.9
    assert report.thresholds[0].threshold == 0.80
    assert report.thresholds[0].both_lists_and_similarity.pairs_reliable == 0.5
    assert report.thresholds[0].similarity_only.pairs_reliable == 0.5


async def test_latency_is_the_p50_and_p95_of_each_query_after_the_warm_up() -> None:
    searcher = TableSearcher({"a": result(), "b": result()})
    readings: list[float] = []
    for query in range(10):
        readings += [float(query), query + (query + 1) / 1000]
    clock = iter(readings)

    report = await measure_latency(searcher, ["a", "b"], queries=10, timer=lambda: next(clock))

    assert report.queries == 10
    assert len(searcher.asked) == 5 + 10
    assert report.p50_ms == pytest.approx(5.5)
    assert report.p95_ms == pytest.approx(9.55)


def test_the_percentile_interpolates_like_numpy() -> None:
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 25.0


async def test_the_full_evaluation_covers_the_three_modes_confidence_and_latency() -> None:
    answer = result(chunk("POL-03", "Prazo"))
    searcher = TableSearcher({"q": answer, "o": result(reliable=False)})

    report = await evaluate(searcher, [QaPair("q", "POL-03", "Prazo")], ["o"])

    assert [m.mode for m in report.modes] == [
        SearchMode.LEXICAL,
        SearchMode.SEMANTIC,
        SearchMode.HYBRID,
    ]
    for mode in report.modes:
        assert mode.by_section.recall_at_3 == 1.0
        assert mode.by_section.mrr == 1.0
        assert mode.by_document.recall_at_3 == 1.0
    assert report.confidence.out_of_scope_unreliable_rate == 1.0
    assert report.latency.queries == 100


def write(tmp_path: Path, name: str, lines: list[object]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def test_the_pairs_are_read_from_json_lines(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "pairs.jsonl",
        [
            {"document": "POL-03", "section": "Prazo", "question": "Quanto tempo?", "id": "1"},
            {"document": "POL-01", "section": "Prazo", "question": "Até quando?", "id": "2"},
        ],
    )

    assert load_pairs(path) == (
        QaPair("Quanto tempo?", "POL-03", "Prazo"),
        QaPair("Até quando?", "POL-01", "Prazo"),
    )


def test_the_out_of_scope_questions_are_read_and_blank_lines_skipped(tmp_path: Path) -> None:
    path = tmp_path / "oos.jsonl"
    path.write_text('{"id": "1", "question": "Previsão do tempo?"}\n\n', encoding="utf-8")

    assert load_out_of_scope(path) == ("Previsão do tempo?",)


def test_the_versioned_evaluation_sets_are_valid() -> None:
    root = Path(__file__).resolve().parents[3] / "data" / "eval"

    pairs = load_pairs(root / "policy_qa.jsonl")
    out_of_scope = load_out_of_scope(root / "out_of_scope.jsonl")

    assert len(pairs) == 60
    assert 25 <= len(out_of_scope) <= 35


VALID = '{"question": "ok", "document": "POL-01", "section": "A"}'
MISSING = "evaluation_data.missing_field"


@pytest.mark.parametrize(
    ("content", "rule", "line"),
    [
        ("", "evaluation_data.empty", 0),
        ("\n\n", "evaluation_data.empty", 0),
        ("not json", "evaluation_data.invalid_json", 1),
        (f"{VALID}\n[1]", "evaluation_data.not_an_object", 2),
        ('{"question": "ok", "document": "POL-01"}', MISSING, 1),
        ('{"question": "  ", "document": "POL-01", "section": "A"}', MISSING, 1),
        ('{"question": 3, "document": "POL-01", "section": "A"}', MISSING, 1),
    ],
)
def test_a_malformed_file_is_refused_with_the_rule_and_the_line(
    tmp_path: Path, content: str, rule: str, line: int
) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(EvaluationDataError) as error:
        load_pairs(path)

    assert (error.value.rule, error.value.line) == (rule, line)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(EvaluationDataError) as error:
        load_pairs(tmp_path / "absent.jsonl")

    assert error.value.rule == "evaluation_data.unreadable"


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "latin.jsonl"
    path.write_bytes('{"question": "é"}'.encode("latin-1"))

    with pytest.raises(EvaluationDataError) as error:
        load_out_of_scope(path)

    assert error.value.rule == "evaluation_data.not_utf8"
