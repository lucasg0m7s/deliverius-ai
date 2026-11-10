"""The final evaluation with fake candidates: one pass per message, every set, the rule applied."""

import json
from pathlib import Path

import numpy as np
import pytest
from support.intent_data import learnable_set
from support.intent_scoring import KeywordScorer
from support.intent_scoring import measured as fake_measured

from deliverius.datagen.records import Intent
from research.intent.candidate import canonical_json
from research.intent.config import load_config
from research.intent.data import INTENTS, Corpus
from research.intent.evaluate import (
    SAMPLE,
    Measured,
    evaluate,
    evaluation_document,
    figures,
    measure_llm,
    table_lines,
)
from research.intent.llm_baseline import BaselineRun, Outcome
from research.intent.metrics import EMPTY
from research.intent.sample import llm_sample

CONFIG = load_config().model_copy(
    update={"rule": load_config().rule.model_copy(update={"bootstrap_resamples": 200})}
)
TESTS = {
    Corpus.TEST: learnable_set(Corpus.TEST, 6),
    Corpus.HOLDOUT: learnable_set(Corpus.HOLDOUT, 6),
    Corpus.HUMAN: learnable_set(Corpus.HUMAN, 10),
}
SAMPLE_IDS = llm_sample([TESTS[Corpus.TEST], TESTS[Corpus.HOLDOUT]], TESTS[Corpus.HUMAN], 2, 1)


def measured(name: str, scorer: KeywordScorer, ms: float, size: int = 1_000) -> Measured:
    return fake_measured(name, scorer, ms, TESTS, warmup=CONFIG.latency.warmup_messages, size=size)


def llm_measured() -> Measured:
    sample = [i for ids in SAMPLE_IDS.values() for i in ids]
    by_corpus = {e.id: e for messages in TESTS.values() for e in messages.examples}
    outcomes = (
        Outcome(sample[0], EMPTY, 0.0, None, 380),
        *(Outcome(i, INTENTS.index(by_corpus[i].intent), 0.9, None, 400) for i in sample[1:]),
    )
    run = BaselineRun(outcomes, dict.fromkeys(sample, 700.0), 0)
    return measure_llm(run, threshold=0.8)


def test_each_message_is_scored_alone_after_the_warm_up() -> None:
    scorer = KeywordScorer()

    result = measured("tfidf", scorer, ms=2.0)

    messages = sum(len(m) for m in TESTS.values())
    assert scorer.calls == CONFIG.latency.warmup_messages + messages
    assert len(result.latency_ms) == messages
    assert np.allclose(result.latency_ms, 2.0)


def test_the_llm_cost_is_per_thousand_messages_and_empties_are_kept() -> None:
    llm = llm_measured()

    assert llm.cost_per_thousand_usd > 0
    assert EMPTY in {label for label, _ in llm.by_id.values()}


def test_the_rule_picks_the_fast_tie_and_refuses_the_slow_and_the_blind(tmp_path: Path) -> None:
    candidates = [
        measured("tfidf", KeywordScorer(), ms=2.0, size=3_000_000),
        measured("e5", KeywordScorer(blind=frozenset({Intent.SENSITIVE})), ms=8.0),
        measured("bertimbau", KeywordScorer(), ms=150.0),
        llm_measured(),
    ]

    evaluation = evaluate(candidates, TESTS, SAMPLE_IDS, CONFIG)

    choice = evaluation.choice
    assert choice.winner == "tfidf"
    assert set(choice.refused["e5"]) == {"sensitive_recall"}
    assert set(choice.refused["bertimbau"]) == {"p95_latency"}
    assert set(choice.refused["llm"]) == {"p95_latency"}
    tfidf = evaluation.approaches["tfidf"]
    assert set(tfidf["sets"]) == {"test", "holdout", "human", SAMPLE}
    assert tfidf["sets"]["human"]["macro_f1"]["point"] == 1.0
    assert tfidf["sensitive_recall_combined"] == 1.0
    assert tfidf["latency_ms"]["p95"] == pytest.approx(2.0)
    llm = evaluation.approaches["llm"]
    assert llm["sets"]["human"]["messages"] == len(TESTS[Corpus.HUMAN])
    assert llm["sets"]["test"]["messages"] == len(SAMPLE_IDS["test"])
    assert llm["sets"][SAMPLE]["messages"] == sum(len(ids) for ids in SAMPLE_IDS.values())
    assert evaluation.human_errors == []


def test_the_document_is_json_and_the_figures_are_drawn(tmp_path: Path) -> None:
    candidates = [measured("tfidf", KeywordScorer(blind=frozenset({Intent.OTHER})), 2.0)]
    evaluation = evaluate(candidates, TESTS, SAMPLE_IDS, CONFIG)

    document = json.loads(canonical_json(evaluation_document(evaluation, {"seed": 1})))
    paths = figures(evaluation, tmp_path)

    assert document["choice"]["winner"] == "tfidf"
    assert {path.name for path in paths} == {"confusion_tfidf_human.png", "coverage_tfidf.png"}
    assert all(path.stat().st_size > 0 for path in paths)
    assert "Vencedor pela regra: tfidf" in table_lines(evaluation)


def test_nobody_eligible_still_reports_the_best_errors() -> None:
    blind = KeywordScorer(blind=frozenset({Intent.SENSITIVE, Intent.CANCEL_ORDER}))

    evaluation = evaluate([measured("tfidf", blind, 2.0)], TESTS, SAMPLE_IDS, CONFIG)

    assert evaluation.choice.winner is None
    assert "Nenhuma abordagem elegível." in table_lines(evaluation)
    assert {e["true"] for e in evaluation.human_errors} == {"sensitive", "cancel_order"}
    assert all(e["predicted"] == "other" for e in evaluation.human_errors)
