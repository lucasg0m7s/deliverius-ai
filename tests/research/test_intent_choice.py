"""Each branch of the choice rule (design D2)."""

import numpy as np

from research.intent.choice import Choice, Contender, Refusal, choose
from research.intent.config import load_config

RULE = load_config().rule.model_copy(update={"bootstrap_resamples": 300})
RNG = np.random.default_rng(0)
HUMAN_Y = RNG.integers(0, 3, 300).astype(np.int64)


def predictions(accuracy: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.where(rng.random(300) < accuracy, HUMAN_Y, (HUMAN_Y + 1) % 3).astype(np.int64)


PERFECT = HUMAN_Y.copy()
NEARLY_PERFECT = predictions(0.99, 1)
POOR = predictions(0.6, 2)


def contender(
    name: str,
    human: np.ndarray,
    *,
    recall: float = 0.97,
    p95: float = 10.0,
    size: int = 1_000,
) -> Contender:
    return Contender(name, recall, p95, size, human)


def run(*contenders: Contender) -> Choice:
    return choose(contenders, HUMAN_Y, 3, RULE, seed=1)


def test_a_sensitive_recall_below_the_floor_is_not_eligible() -> None:
    choice = run(contender("a", PERFECT, recall=0.94), contender("b", POOR))

    assert choice.refused["a"] == (Refusal.SENSITIVE_RECALL,)
    assert choice.eligible == ("b",)
    assert choice.winner == "b"


def test_a_p95_above_the_ceiling_is_not_eligible() -> None:
    choice = run(contender("llm", PERFECT, p95=850.0), contender("tfidf", POOR))

    assert choice.refused["llm"] == (Refusal.LATENCY,)
    assert choice.winner == "tfidf"


def test_a_tie_with_the_best_goes_to_the_faster_one() -> None:
    choice = run(contender("bert", PERFECT, p95=40.0), contender("tfidf", NEARLY_PERFECT, p95=2.0))

    assert choice.best == "bert"
    assert choice.differences["tfidf"].contains_zero
    assert choice.tied_with_best == ("tfidf",)
    assert choice.winner == "tfidf"


def test_a_significant_difference_keeps_the_best() -> None:
    choice = run(contender("bert", PERFECT, p95=40.0), contender("tfidf", POOR, p95=2.0))

    assert not choice.differences["tfidf"].contains_zero
    assert choice.tied_with_best == ()
    assert choice.winner == "bert"


def test_the_size_breaks_a_latency_tie() -> None:
    choice = run(
        contender("e5", PERFECT, p95=5.0, size=120_000_000),
        contender("tfidf", NEARLY_PERFECT, p95=5.0, size=3_000_000),
    )

    assert choice.winner == "tfidf"


def test_nobody_eligible_means_no_winner() -> None:
    choice = run(contender("a", PERFECT, recall=0.5), contender("b", PERFECT, p95=500.0))

    assert choice.winner is None
    assert choice.best is None
    assert choice.eligible == ()
    assert set(choice.refused) == {"a", "b"}
    assert choice.human_f1["a"] == 1.0
