"""How good the search is: recall@3, MRR with a bootstrap interval, confidence, latency.

A pair is a question and the policy section that answers it. A search hits by section when one of
its three chunks has the document and the section of the pair, and by document when one of them
has the document of the pair; the reciprocal rank is `1 / position` of the first hit, or 0. The
questions outside the scope are not used to pick the similarity threshold, so they measure the
refusal honestly.
"""

import json
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import cycle, islice
from pathlib import Path
from typing import Final, Protocol

import numpy as np

from deliverius.retrieval.search import (
    RESULT_SIZE,
    ReliabilityRule,
    SearchHit,
    SearchMode,
    SearchResult,
    is_reliable,
)
from deliverius.shared.errors import DeliveriusError, InvariantError

BOOTSTRAP_RESAMPLES: Final = 1_000
BOOTSTRAP_SEED: Final = 20_261_108
CONFIDENCE_LEVEL: Final = 0.95
LATENCY_QUERIES: Final = 100
LATENCY_WARMUP_QUERIES: Final = 5
# Candidate similarity thresholds shown to pick the configured one: 0.80, 0.81, ... 0.90.
CANDIDATE_THRESHOLDS: Final = tuple(round(0.80 + step / 100, 2) for step in range(11))
_MS_PER_SECOND: Final = 1_000
_PERCENT: Final = 100
_DISTRIBUTION_PERCENTS: Final = (0, 5, 25, 50, 100)


class EvaluationDataError(DeliveriusError):
    """An evaluation file is missing, unreadable or malformed; `line` is from 1, 0 for the file."""

    def __init__(self, rule: str, path: Path, line: int = 0) -> None:
        super().__init__(f"{rule}: {path.name} line {line}")
        self.rule = rule
        self.path = path
        self.line = line


@dataclass(frozen=True, slots=True)
class QaPair:
    question: str
    document: str
    section: str


class Searcher(Protocol):
    async def search(self, query: str, mode: SearchMode) -> SearchResult: ...


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class Score:
    recall_at_3: float
    recall_interval: Interval
    mrr: float
    mrr_interval: Interval


@dataclass(frozen=True, slots=True)
class ModeReport:
    mode: SearchMode
    pairs: int
    by_section: Score
    by_document: Score


@dataclass(frozen=True, slots=True)
class Distribution:
    """Minimum, 5th and 25th percentile, median and maximum of the best similarities."""

    minimum: float
    p5: float
    p25: float
    median: float
    maximum: float


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """What a rule does at one threshold: pairs it trusts, out-of-scope questions it refuses."""

    pairs_reliable: float
    out_of_scope_refused: float


@dataclass(frozen=True, slots=True)
class ThresholdRow:
    threshold: float
    both_lists_and_similarity: RuleOutcome
    similarity_only: RuleOutcome


@dataclass(frozen=True, slots=True)
class ConfidenceReport:
    pairs_unreliable_rate: float
    out_of_scope_unreliable_rate: float
    pairs_similarity: Distribution
    out_of_scope_similarity: Distribution
    pairs_in_both_lists: float
    out_of_scope_in_both_lists: float
    thresholds: tuple[ThresholdRow, ...]


@dataclass(frozen=True, slots=True)
class LatencyReport:
    queries: int
    p50_ms: float
    p95_ms: float


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    modes: tuple[ModeReport, ...]
    confidence: ConfidenceReport
    latency: LatencyReport


def load_pairs(path: Path) -> tuple[QaPair, ...]:
    return tuple(
        QaPair(
            question=_text(record, "question", path, line),
            document=_text(record, "document", path, line),
            section=_text(record, "section", path, line),
        )
        for line, record in _records(path)
    )


def load_out_of_scope(path: Path) -> tuple[str, ...]:
    return tuple(_text(record, "question", path, line) for line, record in _records(path))


def reciprocal_rank(hits: Sequence[SearchHit], pair: QaPair, *, by_document: bool = False) -> float:
    """`1 / position` of the first of the top three in the pair's section (or document); else 0."""
    for position, hit in enumerate(hits[:RESULT_SIZE], start=1):
        if hit.document_id == pair.document and (by_document or hit.section == pair.section):
            return 1 / position
    return 0.0


def bootstrap_interval(
    values: Sequence[float],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    confidence: float = CONFIDENCE_LEVEL,
) -> Interval:
    """Percentile interval of the mean, by resampling `values` with replacement."""
    if not values:
        raise InvariantError("evaluation.bootstrap_empty")
    sample = np.asarray(values, dtype=np.float64)
    generator = np.random.default_rng(seed)
    picks = generator.integers(0, len(sample), size=(resamples, len(sample)))
    means = sample[picks].mean(axis=1)
    tail = (1 - confidence) / 2 * _PERCENT
    low, high = np.percentile(means, [tail, _PERCENT - tail])
    return Interval(float(low), float(high))


def distribution(values: Sequence[float]) -> Distribution:
    if not values:
        raise InvariantError("evaluation.no_similarity")
    return Distribution(*[percentile(values, percent) for percent in _DISTRIBUTION_PERCENTS])


def percentile(values: Sequence[float], percent: float) -> float:
    return float(np.percentile(values, percent))


async def evaluate_mode(
    searcher: Searcher, pairs: Sequence[QaPair], mode: SearchMode
) -> ModeReport:
    results = [(await searcher.search(pair.question, mode)).chunks for pair in pairs]
    return ModeReport(
        mode=mode,
        pairs=len(pairs),
        by_section=_score(
            [reciprocal_rank(hits, pair) for hits, pair in zip(results, pairs, strict=True)]
        ),
        by_document=_score(
            [
                reciprocal_rank(hits, pair, by_document=True)
                for hits, pair in zip(results, pairs, strict=True)
            ]
        ),
    )


async def evaluate_confidence(
    searcher: Searcher, pairs: Sequence[QaPair], out_of_scope: Sequence[str]
) -> ConfidenceReport:
    from_pairs = [await searcher.search(pair.question, SearchMode.HYBRID) for pair in pairs]
    from_others = [await searcher.search(question, SearchMode.HYBRID) for question in out_of_scope]
    return ConfidenceReport(
        pairs_unreliable_rate=_rate(not result.reliable for result in from_pairs),
        out_of_scope_unreliable_rate=_rate(not result.reliable for result in from_others),
        pairs_similarity=distribution(_best_similarities(from_pairs)),
        out_of_scope_similarity=distribution(_best_similarities(from_others)),
        pairs_in_both_lists=_rate(result.in_both_lists for result in from_pairs),
        out_of_scope_in_both_lists=_rate(result.in_both_lists for result in from_others),
        thresholds=tuple(
            ThresholdRow(
                threshold,
                _outcome(
                    ReliabilityRule.BOTH_LISTS_AND_SIMILARITY, threshold, from_pairs, from_others
                ),
                _outcome(ReliabilityRule.SIMILARITY_ONLY, threshold, from_pairs, from_others),
            )
            for threshold in CANDIDATE_THRESHOLDS
        ),
    )


async def measure_latency(
    searcher: Searcher,
    questions: Sequence[str],
    queries: int = LATENCY_QUERIES,
    timer: Callable[[], float] = time.perf_counter,
) -> LatencyReport:
    """p50 and p95 of the whole search (embedding and SQL), one query at a time, after a warm-up."""
    asked = list(islice(cycle(questions), LATENCY_WARMUP_QUERIES + queries))
    for question in asked[:LATENCY_WARMUP_QUERIES]:
        await searcher.search(question, SearchMode.HYBRID)
    seconds: list[float] = []
    for question in asked[LATENCY_WARMUP_QUERIES:]:
        started = timer()
        await searcher.search(question, SearchMode.HYBRID)
        seconds.append(timer() - started)
    milliseconds = [value * _MS_PER_SECOND for value in seconds]
    return LatencyReport(queries, percentile(milliseconds, 50), percentile(milliseconds, 95))


async def evaluate(
    searcher: Searcher, pairs: Sequence[QaPair], out_of_scope: Sequence[str]
) -> EvaluationReport:
    modes = tuple([await evaluate_mode(searcher, pairs, mode) for mode in SearchMode])
    confidence = await evaluate_confidence(searcher, pairs, out_of_scope)
    questions = [pair.question for pair in pairs] + list(out_of_scope)
    return EvaluationReport(modes, confidence, await measure_latency(searcher, questions))


def _score(ranks: Sequence[float]) -> Score:
    hits = [1.0 if rank > 0 else 0.0 for rank in ranks]
    return Score(
        recall_at_3=float(np.mean(hits)),
        recall_interval=bootstrap_interval(hits),
        mrr=float(np.mean(ranks)),
        mrr_interval=bootstrap_interval(ranks),
    )


def _outcome(
    rule: ReliabilityRule,
    threshold: float,
    from_pairs: Sequence[SearchResult],
    from_others: Sequence[SearchResult],
) -> RuleOutcome:
    def trusted(result: SearchResult) -> bool:
        return is_reliable(rule, result.in_both_lists, result.best_similarity, threshold)

    return RuleOutcome(
        pairs_reliable=_rate(trusted(result) for result in from_pairs),
        out_of_scope_refused=_rate(not trusted(result) for result in from_others),
    )


def _best_similarities(results: Sequence[SearchResult]) -> list[float]:
    return [result.best_similarity for result in results if result.best_similarity is not None]


def _rate(flags: Iterable[bool]) -> float:
    values = list(flags)
    return sum(values) / len(values) if values else 0.0


def _records(path: Path) -> Iterator[tuple[int, dict[str, object]]]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise EvaluationDataError("evaluation_data.unreadable", path) from error
    except UnicodeDecodeError as error:
        raise EvaluationDataError("evaluation_data.not_utf8", path) from error
    found = False
    for line, raw in enumerate(content.splitlines(), start=1):
        if not raw.strip():
            continue
        found = True
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as error:
            raise EvaluationDataError("evaluation_data.invalid_json", path, line) from error
        if not isinstance(record, dict):
            raise EvaluationDataError("evaluation_data.not_an_object", path, line)
        yield line, record
    if not found:
        raise EvaluationDataError("evaluation_data.empty", path)


def _text(record: dict[str, object], key: str, path: Path, line: int) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationDataError("evaluation_data.missing_field", path, line)
    return value
